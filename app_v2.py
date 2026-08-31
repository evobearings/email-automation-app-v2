from datetime import datetime, timedelta
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import random
import re
import smtplib
import sqlite3
import time
import imapclient
import pandas as pd
import streamlit as st

# ==========================================
# 1. DATABASE MANAGEMENT (SQLite)
# ==========================================


def init_db():
  conn = sqlite3.connect("campaigns.db")
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            university TEXT,
            department TEXT,
            email TEXT UNIQUE,
            initial_sent_date TIMESTAMP,
            status TEXT DEFAULT 'Pending',
            followup_sent_date TIMESTAMP
        )
    """)
  # Migration checks for existing databases
  cursor.execute("PRAGMA table_info(sent_emails)")
  columns = [col[1] for col in cursor.fetchall()]
  if "university" not in columns:
    cursor.execute(
        "ALTER TABLE sent_emails ADD COLUMN university TEXT DEFAULT ''"
    )
  if "department" not in columns:
    cursor.execute(
        "ALTER TABLE sent_emails ADD COLUMN department TEXT DEFAULT ''"
    )
  conn.commit()
  conn.close()


def log_initial_email(
    name: str, email: str, university: str = "", department: str = ""
):
  conn = sqlite3.connect("campaigns.db")
  cursor = conn.cursor()
  now = datetime.now()
  cursor.execute(
      """
        INSERT INTO sent_emails (name, university, department, email, initial_sent_date, status)
        VALUES (?, ?, ?, ?, ?, 'Pending')
        ON CONFLICT(email) DO UPDATE SET
            name=?,
            university=?,
            department=?,
            initial_sent_date=?,
            status='Pending'
    """,
      (
          name,
          university,
          department,
          email.lower(),
          now,
          name,
          university,
          department,
          now,
      ),
  )
  conn.commit()
  conn.close()


def log_followup_email(email: str):
  conn = sqlite3.connect("campaigns.db")
  cursor = conn.cursor()
  now = datetime.now()
  cursor.execute(
      """
        UPDATE sent_emails
        SET status = 'Follow-up Sent', followup_sent_date = ?
        WHERE LOWER(email) = LOWER(?)
    """,
      (now, email),
  )
  conn.commit()
  conn.close()


def get_emails_sent_in_last_24h() -> set:
  conn = sqlite3.connect("campaigns.db")
  cutoff_time = datetime.now() - timedelta(hours=24)
  cursor = conn.cursor()
  cursor.execute(
      """
        SELECT LOWER(email) FROM sent_emails
        WHERE initial_sent_date >= ? OR followup_sent_date >= ?
    """,
      (cutoff_time, cutoff_time),
  )
  rows = cursor.fetchall()
  conn.close()
  return {row[0] for row in rows}


def update_reply_status(replied_emails: list):
  if not replied_emails:
    return 0
  conn = sqlite3.connect("campaigns.db")
  cursor = conn.cursor()
  count = 0
  for email in replied_emails:
    cursor.execute(
        """
            UPDATE sent_emails
            SET status = 'Replied'
            WHERE LOWER(email) = LOWER(?) AND status != 'Replied'
        """,
        (email,),
    )
    count += cursor.rowcount
  conn.commit()
  conn.close()
  return count


def fetch_followup_candidates(days_threshold=15):
  conn = sqlite3.connect("campaigns.db")
  cutoff_date = datetime.now() - timedelta(days=days_threshold)
  df = pd.read_sql_query(
      """
        SELECT id, name, university, department, email, initial_sent_date, status
        FROM sent_emails
        WHERE status = 'Pending' AND initial_sent_date <= ?
    """,
      conn,
      params=(cutoff_date,),
  )
  conn.close()
  return df


init_db()

# ==========================================
# 2. CONTACT CLEANING & TEMPLATE ENGINE
# ==========================================

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", re.IGNORECASE
)


def load_and_parse_file(uploaded_file):
  # Rewind file pointer to avoid Streamlit rerun crashes
  uploaded_file.seek(0)
  if uploaded_file.name.endswith(".csv"):
    try:
      df = pd.read_csv(uploaded_file)
    except Exception:
      uploaded_file.seek(0)
      df = pd.read_csv(uploaded_file, encoding="latin1")
  else:
    df = pd.read_excel(uploaded_file)

  df = df.fillna("").astype(str)
  df.columns = [str(col).strip() for col in df.columns]

  # 1. Identify Email column FIRST to prevent naming conflicts
  email_col = None
  for col in df.columns:
    if "email" in col.lower() or "mail" in col.lower():
      email_col = col
      break

  if not email_col:
    for col in df.columns:
      if any(EMAIL_REGEX.search(str(val)) for val in df[col]):
        email_col = col
        break

  if email_col:
    df.rename(columns={email_col: "Email"}, inplace=True)
  elif "Email" not in df.columns:
    df["Email"] = ""

  # 2. Map remaining columns without touching Email
  for col in df.columns:
    if col == "Email":
      continue
    c_lower = col.lower()
    if any(k in c_lower for k in ["name", "contact", "person"]):
      df.rename(columns={col: "Name"}, inplace=True)
    elif any(k in c_lower for k in ["university", "college", "institution"]):
      df.rename(columns={col: "University"}, inplace=True)
    elif any(k in c_lower for k in ["dept", "department"]):
      df.rename(columns={col: "Department"}, inplace=True)

  if "Name" not in df.columns:
    df["Name"] = "Valued Partner"
  if "University" not in df.columns:
    df["University"] = "your institution"
  if "Department" not in df.columns:
    df["Department"] = "your department"

  # Deduplicate column names to prevent Arrow/Streamlit crashes
  cols = pd.Series(df.columns)
  for dup in cols[cols.duplicated()].unique():
    cols[cols == dup] = [
        f"{dup}_{i}" if i != 0 else dup for i in range(sum(cols == dup))
    ]
  df.columns = cols

  # Filter valid emails
  df["Email"] = df["Email"].str.strip().str.lower()
  df = df[df["Email"].str.contains("@", na=False)].copy()

  # Apply 24h safeguard
  recent_emails = get_emails_sent_in_last_24h()
  initial_len = len(df)
  df = df[~df["Email"].isin(recent_emails)].copy()
  skipped_count = initial_len - len(df)

  return df, skipped_count


def render_template(template_str: str, row_dict: dict) -> str:
  rendered = template_str
  for key, value in row_dict.items():
    placeholder = f"{{{key}}}"
    if placeholder in rendered:
      rendered = rendered.replace(placeholder, str(value))
  return rendered


# ==========================================
# 3. IMAP AUTO-REPLY DETECTOR
# ==========================================


def check_inbox_for_replies(
    user_email, user_password, imap_server="imap.gmail.com"
):
  replied_senders = set()
  try:
    with imapclient.IMAPClient(imap_server, ssl=True) as client:
      client.login(user_email, user_password)
      client.select_folder("INBOX")

      since_date = (datetime.now() - timedelta(days=60)).strftime("%d-%b-%Y")
      messages = client.search(["SINCE", since_date])

      if not messages:
        return [], None

      fetch_data = client.fetch(messages, ["ENVELOPE"])
      for msg_id, data in fetch_data.items():
        envelope = data.get(b"ENVELOPE") or data.get("ENVELOPE")
        if envelope and envelope.from_:
          for addr in envelope.from_:
            mailbox = (
                addr.mailbox.decode("utf-8", errors="ignore")
                if isinstance(addr.mailbox, bytes)
                else (addr.mailbox or "")
            )
            host = (
                addr.host.decode("utf-8", errors="ignore")
                if isinstance(addr.host, bytes)
                else (addr.host or "")
            )
            if mailbox and host:
              replied_senders.add(f"{mailbox}@{host}".lower())

    return list(replied_senders), None
  except Exception as e:
    return [], str(e)


# ==========================================
# 4. EMAIL FORMATTER (HTML Builder)
# ==========================================


def build_html_body(
    text_content,
    gdrive_links=None,
    catalog_links=None,
    video_links=None,
    video_names=None,
):
  formatted_text = text_content.replace("\n", "<br>")
  links_html = ""
  highlight_style = "font-weight: bold; background-color: #fffacd; padding: 3px 6px; border-radius: 4px; text-decoration: underline;"

  if gdrive_links:
    drive_items = "".join([
        f'<li style="margin-bottom: 8px;"><a href="{l.strip()}" target="_blank"'
        f' style="color: #1a73e8; {highlight_style}">Google Drive File'
        f' #{i}</a></li>'
        for i, l in enumerate(gdrive_links, 1)
        if l and l.strip()
    ])
    if drive_items:
      links_html += f'<div style="margin-top: 15px;">📁 <b>Drive Links:</b><ul style="margin: 5px 0 0 20px; padding: 0;">{drive_items}</ul></div>'

  if catalog_links:
    cat_items = "".join([
        f'<li style="margin-bottom: 8px;"><a href="{l.strip()}" target="_blank"'
        f' style="color: #008080; {highlight_style}">Resource Link'
        f' #{i}</a></li>'
        for i, l in enumerate(catalog_links, 1)
        if l and l.strip()
    ])
    if cat_items:
      links_html += f'<div style="margin-top: 15px;">📖 <b>Resources:</b><ul style="margin: 5px 0 0 20px; padding: 0;">{cat_items}</ul></div>'

  if video_links:
    vid_items = "".join([
        f'<li style="margin-bottom: 8px;"><a href="{l.strip()}" target="_blank"'
        f' style="color: #d9534f; {highlight_style}">Video Link #{i}</a></li>'
        for i, l in enumerate(video_links, 1)
        if l and l.strip()
    ])
    if vid_items:
      links_html += f'<div style="margin-top: 15px;">🎥 <b>Video Links:</b><ul style="margin: 5px 0 0 20px; padding: 0;">{vid_items}</ul></div>'

  if video_names:
    v_items = "".join([
        f'<li style="margin-bottom: 8px;"><span style="{highlight_style}">🎥'
        f' {v} (Attached)</span></li>'
        for v in video_names
    ])
    links_html += f'<div style="margin-top: 15px;">▶️ <b>Video Attachments:</b><ul style="margin: 5px 0 0 20px; padding: 0;">{v_items}</ul></div>'

  return f"""
    <html>
        <body style="font-family: Arial, sans-serif; font-size: 14px; color: #333333; line-height: 1.6;">
            <div>{formatted_text}</div>
            {links_html}
        </body>
    </html>
    """


# ==========================================
# 5. STREAMLIT INTERFACE & CONTROLS
# ==========================================

st.set_page_config(
    page_title="Outreach Portal (v2)", page_icon="📧", layout="wide"
)

st.title("📧 Outreach & CRM Portal (v2)")

if "is_sending" not in st.session_state:
  st.session_state.is_sending = False

# --- SIDEBAR CONFIGURATION ---
st.sidebar.header("⚙️ Sender Credentials")
sender_name = st.sidebar.text_input(
    "Sender Display Name", value="EVO Network Bharat"
)
sender_email = st.sidebar.text_input("Sender Email", value="evoalpha30@gmail.com")
sender_password = st.sidebar.text_input(
    "App Password (16-char)", type="password"
)
cc_email = st.sidebar.text_input(
    "CC Recipient (Optional)", value="lakshyarajdevgurujaiswal@gmail.com"
)

smtp_server = st.sidebar.text_input("SMTP Server", value="smtp.gmail.com")
smtp_port = st.sidebar.number_input("SMTP Port", value=587)
use_ssl = st.sidebar.checkbox(
    "Use SSL Connection (Port 465)",
    value=False,
    help="Uncheck to use Port 587 with STARTTLS (Required on Render)",
)
imap_server = st.sidebar.text_input("IMAP Server", value="imap.gmail.com")

tab1, tab2, tab3 = st.tabs(
    ["🚀 Launch Campaign", "⏰ Follow-Up Manager (15 Days)", "📊 Database Log"]
)

# --- TAB 1: LAUNCH CAMPAIGN ---
with tab1:
  st.header("1. Upload Recipient Data")
  uploaded_file = st.file_uploader(
      "Upload Excel (.xlsx, .xls) or CSV",
      type=["xlsx", "xls", "csv"],
      key="up1",
  )

  if uploaded_file:
    df_clean, skipped_count = load_and_parse_file(uploaded_file)
    st.success(f"Extracted {len(df_clean)} valid recipient records.")

    if skipped_count > 0:
      st.warning(
          f"🛡️ **24-Hour Safeguard:** Skipped {skipped_count} recipient(s)"
          " already emailed in the past 24 hours."
      )

    st.markdown("##### 💡 Dynamic Placeholders Ready to Use")
    tags_display = " ".join([f"`{{{col}}}`" for col in df_clean.columns])
    st.markdown(tags_display)

    with st.expander("Preview Recipient Data Table"):
      st.dataframe(df_clean)

    st.header("2. Email Pitch Template")

    sub_val = st.text_input(
        "Subject Line",
        value="Bringing browser-based quantum computing to {University}",
        key="single_sub",
    )

    body_val = st.text_area(
        "Email Body",
        value="""Dear {Name},

I'm Lakshya Raj Devguru Jaiswal, and I'd like to introduce India Bearings & Mill Stores and EVO Bearings and Machineries Pvt. Ltd. — reliable industrial suppliers serving tube and steel manufacturers across Kolkata.

WHAT WE SUPPLY
- Bearings (All Types)
- V-Belts, Chains & Sprockets
- Plummer Blocks, UC Pillow Blocks, Adaptor Sleeves
- Crusher Spares & Accessories
- Rollers, Grease & Lubricants

AUTHORIZED DISTRIBUTORS FOR
EVO Bearings | Renold | Max Spares | KYK Japan | Toyo Power | JK Fenner

Given Lal Baba Seamless Tubes' manufacturing operations, dependable bearing and component supply is likely a core, recurring need — I'd welcome the opportunity to support that.

INTRODUCTORY OFFER
As a welcome gesture, we're offering ₹1,000 cashback on your first order with us.

Happy to set up a quick call at your convenience.

Warm regards,
Lakshya Raj Devguru Jaiswal
India Bearings & Mill Stores | EVO Bearings and Machineries Pvt. Ltd.
+91 9831356591 | +91 9088363391 | +91 9038666911
Website: https://evomachinery.in/""",
        height=380,
        key="single_body",
    )

    with st.expander("📎 Attachments & Media Links", expanded=False):
      gdrive_link_1 = st.text_input(
          "Drive Link", placeholder="https://drive.google.com/...", key="gd1"
      )
      catalog_link_1 = st.text_input(
          "Resource Link", placeholder="https://yourwebsite.com/...", key="cat1"
      )
      video_link_1 = st.text_input(
          "Video Link", placeholder="https://youtube.com/...", key="vlink1"
      )
      video_attachments = st.file_uploader(
          "Attach Videos",
          type=["mp4", "mov", "avi"],
          accept_multiple_files=True,
          key="vid_att1",
      )
      st.markdown("---")
      attachments = st.file_uploader(
          "Attach Documents/PDFs", accept_multiple_files=True, key="att1"
      )

    st.header("3. Campaign Controls")
    delay_range = st.slider(
        "⏱️ Delay Between Emails (Seconds):",
        min_value=5,
        max_value=300,
        value=(30, 60),
        step=1,
        key="delay1",
    )

    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
      start_clicked = st.button(
          "🚀 Start Campaign",
          disabled=st.session_state.is_sending,
          key="start1",
      )
    with col_btn2:
      stop_clicked = st.button("🛑 Stop Campaign", key="stop1")

    if stop_clicked:
      st.session_state.is_sending = False
      st.warning("Campaign halted.")

    if start_clicked:
      if not sender_email or not sender_password:
        st.error("Please enter Sender Email and App Password in the sidebar.")
      elif df_clean.empty:
        st.error("No valid recipient records found.")
      else:
        st.session_state.is_sending = True
        server = None
        try:
          if use_ssl:
            server = smtplib.SMTP_SSL(smtp_server, int(smtp_port), timeout=120)
          else:
            server = smtplib.SMTP(smtp_server, int(smtp_port), timeout=120)
            server.starttls()

          server.login(sender_email, sender_password)

          progress = st.progress(0)
          status_msg = st.empty()
          total = len(df_clean)
          min_sec, max_sec = delay_range

          for i, (_, row) in enumerate(df_clean.iterrows()):
            if not st.session_state.is_sending:
              status_msg.warning("Interrupted.")
              break

            row_dict = row.to_dict()
            row_dict["sender_name"] = sender_name
            row_dict["Name"] = (
                str(row_dict.get("Name", "")).strip() or "Valued Partner"
            )
            row_dict["University"] = (
                str(row_dict.get("University", "")).strip()
                or "your institution"
            )
            row_dict["Department"] = (
                str(row_dict.get("Department", "")).strip() or "your department"
            )

            recipient_email = str(row_dict.get("Email", "")).strip()
            recipient_name = row_dict["Name"]
            recipient_univ = row_dict["University"]
            recipient_dept = row_dict["Department"]

            rendered_body = render_template(body_val, row_dict)
            rendered_subject = render_template(sub_val, row_dict)

            html_body = build_html_body(
                rendered_body,
                [gdrive_link_1],
                [catalog_link_1],
                [video_link_1],
                [v.name for v in video_attachments]
                if video_attachments
                else [],
            )

            msg = MIMEMultipart("mixed")
            msg["From"] = f"{sender_name} <{sender_email}>"
            msg["To"] = recipient_email
            if cc_email.strip():
              msg["Cc"] = cc_email.strip()

            # Fix: UTF-8 Subject Header Encoding
            msg["Subject"] = Header(rendered_subject, "utf-8").encode()

            msg_alt = MIMEMultipart("alternative")
            # Fix: UTF-8 Body Encoding for Emoticons & Unicode
            msg_alt.attach(MIMEText(html_body, "html", "utf-8"))
            msg.attach(msg_alt)

            # Attach Documents
            if attachments:
              for att in attachments:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(att.getvalue())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{att.name}"',
                )
                msg.attach(part)

            # Attach Videos
            if video_attachments:
              for vid in video_attachments:
                vid_part = MIMEBase("video", "octet-stream")
                vid_part.set_payload(vid.getvalue())
                encoders.encode_base64(vid_part)
                vid_part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{vid.name}"',
                )
                msg.attach(vid_part)

            recipients_list = [recipient_email]
            if cc_email.strip():
              recipients_list.append(cc_email.strip())

            server.sendmail(sender_email, recipients_list, msg.as_string())
            log_initial_email(
                recipient_name,
                recipient_email,
                recipient_univ,
                recipient_dept,
            )

            actual_delay = round(random.uniform(min_sec, max_sec), 1)
            status_msg.text(
                f"[{i+1}/{total}] Sent to: {recipient_email} | Subject:"
                f' "{rendered_subject}" (Waiting {actual_delay}s)'
            )
            progress.progress((i + 1) / total)
            time.sleep(actual_delay)

          if st.session_state.is_sending:
            st.success("🎉 Campaign completed successfully!")
          st.session_state.is_sending = False

        except Exception as e:
          st.error(f"Error during sending: {e}")
          st.session_state.is_sending = False
        finally:
          if server:
            try:
              server.quit()
            except Exception:
              pass

# --- TAB 2: FOLLOW-UP MANAGER ---
with tab2:
  st.header("⏰ Auto Follow-Up Engine")
  col1, col2 = st.columns(2)
  with col1:
    days_thresh = st.number_input(
        "Days Without Response", value=15, min_value=0, key="days_th"
    )
  with col2:
    if st.button("🔄 Sync Inbox & Detect Replies (IMAP)", key="sync_btn"):
      if not sender_email or not sender_password:
        st.error("Provide credentials in sidebar.")
      else:
        senders, err = check_inbox_for_replies(
            sender_email, sender_password, imap_server
        )
        if err:
          st.error(f"IMAP Sync Failed: {err}")
        else:
          c = update_reply_status(senders)
          st.success(f"Sync Complete! Updated {c} record(s) with replies.")

  candidates = fetch_followup_candidates(days_threshold=days_thresh)
  st.subheader(
      f"Pending Follow-Ups (Sent >= {days_thresh} days ago without reply)"
  )
  st.dataframe(candidates)

  if not candidates.empty:
    fu_subject = st.text_input(
        "Follow-Up Subject Line",
        value="Following up on our proposal for {University}",
        key="main_fs1",
    )
    fu_body = st.text_area(
        "Follow-Up Body Template",
        value=(
            "Hi {Name},\n\nI am following up on my previous message regarding"
            " Alpha ParadoxQC for {University}. Let me know if you would be open"
            " to connecting.\n\nBest regards,\n{sender_name}"
        ),
        height=150,
        key="body2",
    )

    fu_delay_range = st.slider(
        "⏱️ Delay Between Follow-Ups (seconds):",
        min_value=5,
        max_value=300,
        value=(30, 60),
        key="delay2",
    )

    if st.button("🚀 Start Follow-Ups", key="start2"):
      server = None
      try:
        if use_ssl:
          server = smtplib.SMTP_SSL(smtp_server, int(smtp_port), timeout=120)
        else:
          server = smtplib.SMTP(smtp_server, int(smtp_port), timeout=120)
          server.starttls()

        server.login(sender_email, sender_password)

        progress_fu = st.progress(0)
        status_fu = st.empty()
        total_fu = len(candidates)

        for i, (_, row) in enumerate(candidates.iterrows()):
          rec = str(row["email"]).strip()

          row_dict = row.to_dict()
          row_dict["sender_name"] = sender_name
          row_dict["Name"] = (
              str(row_dict.get("name", "")).strip()
              or str(row_dict.get("Name", "")).strip()
              or "Valued Partner"
          )
          row_dict["University"] = (
              str(row_dict.get("university", "")).strip()
              or str(row_dict.get("University", "")).strip()
              or "your institution"
          )
          row_dict["Department"] = (
              str(row_dict.get("department", "")).strip()
              or str(row_dict.get("Department", "")).strip()
              or "your department"
          )

          rendered_fu_body = render_template(fu_body, row_dict)
          rendered_fu_subject = render_template(fu_subject, row_dict)

          html_b = build_html_body(rendered_fu_body)

          msg = MIMEMultipart("alternative")
          msg["From"] = f"{sender_name} <{sender_email}>"
          msg["To"] = rec
          if cc_email.strip():
            msg["Cc"] = cc_email.strip()

          msg["Subject"] = Header(rendered_fu_subject, "utf-8").encode()
          msg.attach(MIMEText(html_b, "html", "utf-8"))

          recipients_list = [rec]
          if cc_email.strip():
            recipients_list.append(cc_email.strip())

          server.sendmail(sender_email, recipients_list, msg.as_string())
          log_followup_email(rec)

          actual_delay = round(
              random.uniform(fu_delay_range[0], fu_delay_range[1]), 1
          )
          status_fu.text(
              f"[{i+1}/{total_fu}] Follow-up sent to: {rec} (Waiting"
              f" {actual_delay}s)"
          )
          progress_fu.progress((i + 1) / total_fu)
          time.sleep(actual_delay)

        st.success("🎉 Follow-ups sent successfully!")
      except Exception as e:
        st.error(f"Error sending follow-ups: {e}")
      finally:
        if server:
          try:
            server.quit()
          except Exception:
            pass

# --- TAB 3: DATABASE LOG ---
with tab3:
  st.header("📊 Campaign History & Records")
  conn = sqlite3.connect("campaigns.db")
  all_logs = pd.read_sql_query(
      "SELECT * FROM sent_emails ORDER BY id DESC", conn
  )
  conn.close()
  st.dataframe(all_logs)
