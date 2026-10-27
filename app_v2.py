from datetime import datetime, timedelta
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import random
import re
import smtplib
import time
import imapclient
import pandas as pd
import psycopg2
import streamlit as st

# ==========================================
# 1. DATABASE MANAGEMENT (Supabase PostgreSQL)
# ==========================================

DB_URL = st.secrets.get("DATABASE_URL", "")


def get_db_connection():
  if not DB_URL:
    st.error(
        "❌ DATABASE_URL secret is missing in Streamlit Cloud. Please add it in"
        " App Settings -> Secrets."
    )
    st.stop()

  # Enforce SSL connection for Supabase
  if "sslmode" not in DB_URL:
    url = (
        f"{DB_URL}?sslmode=require"
        if "?" not in DB_URL
        else f"{DB_URL}&sslmode=require"
    )
  else:
    url = DB_URL

  return psycopg2.connect(url, connect_timeout=10)


def init_db():
  if not DB_URL:
    return
  conn = get_db_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
            CREATE TABLE IF NOT EXISTS sent_emails (
                id SERIAL PRIMARY KEY,
                name TEXT,
                company TEXT DEFAULT '',
                designation TEXT DEFAULT '',
                department TEXT DEFAULT '',
                city TEXT DEFAULT '',
                university TEXT DEFAULT '',
                email TEXT UNIQUE,
                initial_sent_date TIMESTAMP,
                status TEXT DEFAULT 'Pending',
                followup_sent_date TIMESTAMP
            );
        """)
    conn.commit()
    cursor.close()
  except Exception as e:
    st.error(f"Failed to connect to Supabase database: {e}")
  finally:
    conn.close()


def log_initial_email(
    name: str,
    email: str,
    company: str = "",
    designation: str = "",
    department: str = "",
    city: str = "",
):
  if not DB_URL:
    return
  conn = get_db_connection()
  try:
    cursor = conn.cursor()
    now = datetime.now()

    query = """
            INSERT INTO sent_emails (name, company, designation, department, city, email, initial_sent_date, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'Pending')
            ON CONFLICT(email) DO UPDATE SET
                name = EXCLUDED.name,
                company = EXCLUDED.company,
                designation = EXCLUDED.designation,
                department = EXCLUDED.department,
                city = EXCLUDED.city,
                initial_sent_date = EXCLUDED.initial_sent_date,
                status = 'Pending';
        """
    cursor.execute(
        query,
        (
            name,
            company,
            designation,
            department,
            city,
            email.lower(),
            now,
        ),
    )
    conn.commit()
    cursor.close()
  finally:
    conn.close()


def log_followup_email(email: str):
  if not DB_URL:
    return
  conn = get_db_connection()
  try:
    cursor = conn.cursor()
    now = datetime.now()
    cursor.execute(
        """
            UPDATE sent_emails
            SET status = 'Follow-up Sent', followup_sent_date = %s
            WHERE LOWER(email) = LOWER(%s)
        """,
        (now, email),
    )
    conn.commit()
    cursor.close()
  finally:
    conn.close()


def get_emails_sent_in_last_24h() -> set:
  if not DB_URL:
    return set()
  conn = get_db_connection()
  try:
    cutoff_time = datetime.now() - timedelta(hours=24)
    cursor = conn.cursor()
    cursor.execute(
        """
            SELECT LOWER(email) FROM sent_emails
            WHERE initial_sent_date >= %s OR followup_sent_date >= %s
        """,
        (cutoff_time, cutoff_time),
    )
    rows = cursor.fetchall()
    cursor.close()
    return {row[0] for row in rows}
  finally:
    conn.close()


def update_reply_status(replied_emails: list):
  if not replied_emails or not DB_URL:
    return 0
  conn = get_db_connection()
  try:
    cursor = conn.cursor()
    count = 0
    for email in replied_emails:
      cursor.execute(
          """
                UPDATE sent_emails
                SET status = 'Replied'
                WHERE LOWER(email) = LOWER(%s) AND status != 'Replied'
            """,
          (email,),
      )
      count += cursor.rowcount
    conn.commit()
    cursor.close()
    return count
  finally:
    conn.close()


def fetch_followup_candidates(days_threshold=15):
  if not DB_URL:
    return pd.DataFrame()
  conn = get_db_connection()
  try:
    cutoff_date = datetime.now() - timedelta(days=days_threshold)
    df = pd.read_sql_query(
        """
            SELECT id, name, company, designation, department, city, email, initial_sent_date, status
            FROM sent_emails
            WHERE status = 'Pending' AND initial_sent_date <= %s
        """,
        conn,
        params=(cutoff_date,),
    )
    return df
  finally:
    conn.close()


init_db()

# ==========================================
# 2. CONTACT CLEANING & TAG PARSER ENGINE
# ==========================================

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", re.IGNORECASE
)


def load_and_parse_file(uploaded_file):
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

  # 1. Identify Email column FIRST
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

  # 2. Universal Column Mapping Rules
  mapping_rules = {
      "Name": [
          "name",
          "contact",
          "person",
          "recipient",
          "client",
          "manager",
          "purchaser",
      ],
      "Company": [
          "company",
          "organization",
          "firm",
          "industry",
          "factory",
          "plant",
          "mill",
          "business",
          "university",
          "college",
          "institution",
      ],
      "Designation": [
          "designation",
          "role",
          "title",
          "position",
          "post",
          "job",
      ],
      "Department": ["dept", "department", "division", "section"],
      "City": ["city", "location", "town", "place", "area", "zone"],
      "State": ["state", "region", "province"],
      "Product": ["product", "item", "bearing", "spares", "machine"],
      "Requirement": ["requirement", "need", "demand", "specification"],
  }

  for target_tag, keywords in mapping_rules.items():
    if target_tag in df.columns:
      continue
    for col in df.columns:
      if col == "Email":
        continue
      c_lower = col.lower()
      if any(k in c_lower for k in keywords):
        df.rename(columns={col: target_tag}, inplace=True)
        break

  # Set defaults for core tags
  if "Name" not in df.columns:
    df["Name"] = "Valued Partner"
  if "Company" not in df.columns:
    df["Company"] = "your organization"
  if "Designation" not in df.columns:
    df["Designation"] = "Purchase Manager"
  if "Department" not in df.columns:
    df["Department"] = "Procurement & Maintenance"
  if "City" not in df.columns:
    df["City"] = "your area"

  # Safe Column Deduplication
  new_cols = []
  seen = {}
  for col in df.columns:
    if col in seen:
      seen[col] += 1
      new_cols.append(f"{col}_{seen[col]}")
    else:
      seen[col] = 0
      new_cols.append(col)
  df.columns = new_cols

  # --- BULLETPROOF MULTI-EMAIL SANITIZER ---
  extracted_email = df["Email"].str.extract(
      r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})"
  )[0]
  df["Email"] = extracted_email.fillna("").str.strip().str.lower()

  # Clean & filter valid emails
  df = df[df["Email"].str.contains("@", na=False)].copy()

  # 24-hour safeguard
  recent_emails = get_emails_sent_in_last_24h()
  initial_len = len(df)
  df = df[~df["Email"].isin(recent_emails)].copy()
  skipped_count = initial_len - len(df)

  return df, skipped_count


def render_template(template_str: str, row_dict: dict) -> str:
  """Case-insensitive tag engine supports {tag}, {Tag}, and {TAG}."""
  rendered = template_str
  for key, value in row_dict.items():
    val_str = str(value).strip() if value is not None else ""

    if not val_str:
      k_lower = key.lower()
      if k_lower == "name":
        val_str = "Valued Partner"
      elif k_lower in ["company", "university"]:
        val_str = "your organization"
      elif k_lower in ["city", "location"]:
        val_str = "your area"
      else:
        val_str = ""

    rendered = rendered.replace(f"{{{key}}}", val_str)
    rendered = rendered.replace(f"{{{key.lower()}}}", val_str)
    rendered = rendered.replace(f"{{{key.capitalize()}}}", val_str)
    rendered = rendered.replace(f"{{{key.upper()}}}", val_str)

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
# 4. DYNAMIC HTML EMAIL BUILDER
# ==========================================


def build_html_body(text_content, custom_links=None, video_names=None):
  formatted_text = text_content.replace("\n", "<br>")
  links_html = ""
  highlight_style = "font-weight: bold; background-color: #fffacd; padding: 3px 6px; border-radius: 4px; text-decoration: underline;"

  if custom_links:
    link_items = "".join([
        f'<li style="margin-bottom: 8px;"><a href="{l["url"].strip()}"'
        ' target="_blank" style="color: #1a73e8;'
        f' {highlight_style}">{l["label"].strip() or "Resource Link"}</a></li>'
        for l in custom_links
        if l.get("url") and l["url"].strip()
    ])
    if link_items:
      links_html += f'<div style="margin-top: 15px;">🔗 <b>Important Links & Resources:</b><ul style="margin: 5px 0 0 20px; padding: 0;">{link_items}</ul></div>'

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
    page_title="Outreach & CRM Portal", page_icon="📧", layout="wide"
)

st.title("📧 Outreach & CRM Portal")

if not DB_URL:
  st.error(
      "⚠️ Database connection secret missing! Please add DATABASE_URL to your"
      " Streamlit Cloud secrets."
  )

if "is_sending" not in st.session_state:
  st.session_state.is_sending = False

# Session State for Dynamic Links
if "custom_links" not in st.session_state:
  st.session_state.custom_links = [
      {"label": "Website & Product Catalog", "url": "https://evomachinery.in/"}
  ]

# Session State for Subject Variations
if "subject_variations" not in st.session_state:
  st.session_state.subject_variations = [
      "Bearings & industrial supply partnership",
      "Reliable bearing & component supply for {Company}",
      "Industrial supply partnership opportunity — {Company}",
  ]

# Session State for Multi-Account Rotation
if "sender_accounts" not in st.session_state:
  st.session_state.sender_accounts = [
      {"email": "evobearings@gmail.com", "password": ""}
  ]

# --- SIDEBAR CONFIGURATION ---
st.sidebar.header("⚙️ Sender Credentials & Accounts")
sender_name = st.sidebar.text_input(
    "Sender Display Name",
    value="Lakshya Raj Devguru Jaiswal | EVO Bearings & Mill Stores",
)

st.sidebar.markdown("##### 👥 Multi-Account Rotation")
st.sidebar.caption(
    "Add multiple accounts to bypass daily Gmail limits. The engine will"
    " automatically rotate accounts during campaigns."
)

updated_accounts = []
for idx, acc in enumerate(st.session_state.sender_accounts):
  st.sidebar.markdown(f"**Account #{idx+1}**")
  col_acc_e, col_acc_p = st.sidebar.columns([1, 1])
  with col_acc_e:
    acc_e = st.text_input(
        f"Email #{idx+1}",
        value=acc["email"],
        key=f"acc_email_{idx}",
        placeholder="email@gmail.com",
    )
  with col_acc_p:
    acc_p = st.text_input(
        f"App Password #{idx+1}",
        value=acc["password"],
        type="password",
        key=f"acc_pass_{idx}",
        placeholder="16-char code",
    )

  updated_accounts.append({"email": acc_e, "password": acc_p})

st.session_state.sender_accounts = updated_accounts

if len(st.session_state.sender_accounts) > 1:
  if st.sidebar.button("❌ Remove Last Account"):
    st.session_state.sender_accounts.pop()
    st.rerun()

if st.sidebar.button("➕ Add Sender Account"):
  st.session_state.sender_accounts.append({"email": "", "password": ""})
  st.rerun()

emails_per_account_limit = st.sidebar.number_input(
    "Max Emails Per Account Before Rotation",
    min_value=1,
    max_value=500,
    value=100,
    help=(
        "The engine automatically switches to the next account after sending"
        " this many emails."
    ),
)

st.sidebar.markdown("---")

# Optional CC Toggle (Disabled by default)
enable_cc = st.sidebar.checkbox(
    "Enable CC Recipient",
    value=False,
    help=(
        "Check to send a copy of every outgoing email to a CC address (Note:"
        " Uses extra daily sending quota)"
    ),
)

if enable_cc:
  cc_email = st.sidebar.text_input(
      "CC Recipient Email", value="lakshyarajdevgurujaiswal@gmail.com"
  )
else:
  cc_email = ""

smtp_server = st.sidebar.text_input("SMTP Server", value="smtp.gmail.com")
smtp_port = st.sidebar.number_input("SMTP Port", value=587)
use_ssl = st.sidebar.checkbox(
    "Use SSL Connection (Port 465)",
    value=False,
    help="Uncheck to use Port 587 with STARTTLS (Recommended)",
)
imap_server = st.sidebar.text_input("IMAP Server", value="imap.gmail.com")

tab1, tab2, tab3 = st.tabs(
    ["🚀 Launch Campaign", "⏰ Follow-Up Manager (15 Days)", "📊 Database Log"]
)


def connect_smtp_account(account):
  if use_ssl:
    server = smtplib.SMTP_SSL(smtp_server, int(smtp_port), timeout=120)
  else:
    server = smtplib.SMTP(smtp_server, int(smtp_port), timeout=120)
    server.starttls()
  server.login(account["email"].strip(), account["password"].strip())
  return server


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

    st.markdown("##### 💡 Dynamic Placeholders Extracted from Your Sheet")
    tags_display = " ".join([f"`{{{col}}}`" for col in df_clean.columns])
    st.markdown(tags_display)
    st.caption(
        "You can use any tag above inside your Subject line or Email body!"
    )

    with st.expander("Preview Parsed Data Table"):
      st.dataframe(df_clean)

  st.header("2. Email Pitch Template")

  use_rotation = st.checkbox(
      "🎲 Enable Subject Line Mixer / Random Variations (Prevents Spam"
      " Filtering)",
      value=False,
      key="chk_sub_rot",
      help=(
          "When enabled, a random subject line from your list below will be"
          " selected for each recipient."
      ),
  )

  active_subjects = []

  if not use_rotation:
    sub_val = st.text_input(
        "Subject Line",
        value="Bearings & industrial supply partnership",
        key="single_sub",
        help=(
            "Subject Line. You can append placeholders like: Bearings &"
            " industrial supply partnership - {Company}"
        ),
    )
    active_subjects = (
        [sub_val] if sub_val.strip() else ["Bearings & industrial supply partnership"]
    )
  else:
    st.markdown("##### 🎲 Subject Line Variations (Max 5)")
    st.caption(
        "Customize your subjects below. The engine will randomly assign one per"
        " recipient to bypass spam filters."
    )

    updated_subjects = []
    for idx, sub_item in enumerate(st.session_state.subject_variations):
      col_sub, col_del = st.columns([6, 0.6])
      with col_sub:
        s_val = st.text_input(
            f"Subject Variation #{idx+1}",
            value=sub_item,
            key=f"sub_var_{idx}",
            placeholder=(
                "e.g. Bearings & industrial supply partnership - {Company}"
            ),
        )
      with col_del:
        st.write("")
        st.write("")
        if st.button("❌", key=f"del_sub_{idx}"):
          st.session_state.subject_variations.pop(idx)
          st.rerun()
      updated_subjects.append(s_val)

    st.session_state.subject_variations = updated_subjects
    active_subjects = [
        s for s in st.session_state.subject_variations if s.strip()
    ]

    if len(st.session_state.subject_variations) < 5:
      if st.button("➕ Add Subject Variation"):
        st.session_state.subject_variations.append("")
        st.rerun()
    else:
      st.info("Maximum limit of 5 subject variations reached.")

  body_val = st.text_area(
      "Email Body",
      value="""Dear {Name},

I am Lakshya Raj Devguru Jaiswal, representing India Bearings & Mill Stores and EVO Bearings and Machineries Pvt. Ltd. — reliable industrial suppliers serving manufacturing plants, steel mills, tube manufacturers, and heavy industrial facilities across {City}.

WHAT WE SUPPLY
- Bearings (Ball, Taper Roller, Spherical, Needle & Pillow Blocks)
- V-Belts, Chains & Sprockets
- Plummer Blocks, Adaptor Sleeves & Couplings
- Crusher Spares & Mechanical Components
- Industrial Rollers, High-Temp Greases & Lubricants

AUTHORIZED DISTRIBUTORS & STOCKISTS FOR
EVO Bearings | Renold | Max Spares | KYK Japan | Toyo Power | JK Fenner

Given {Company}'s manufacturing operations, dependable bearing and mechanical component supply is likely a core, recurring requirement for your maintenance and production lines — I would welcome the opportunity to support {Company} with competitive pricing and rapid dispatch.

INTRODUCTORY OFFER FOR NEW PARTNERS
As a welcome gesture for {Company}, we are offering ₹1,000 cashback on your first trial order with us.

Please let me know if we can share our complete product catalog or schedule a brief introductory call.

Warm regards,
Lakshya Raj Devguru Jaiswal
India Bearings & Mill Stores | EVO Bearings and Machineries Pvt. Ltd.
+91 9831356591 | +91 9088363391 | +91 9038666911
Website: https://evomachinery.in/""",
      height=380,
      key="single_body",
  )

  with st.expander("📎 Dynamic Links & File Attachments", expanded=False):
    st.markdown("##### 🔗 Custom Hyperlinks")
    st.caption(
        "Customize the link name and add as many links as you want using the ➕"
        " button."
    )

    updated_links = []
    for idx, link_item in enumerate(st.session_state.custom_links):
      col_label, col_url, col_del = st.columns([2.5, 4, 0.6])
      with col_label:
        lbl = st.text_input(
            f"Link Name #{idx+1}",
            value=link_item["label"],
            key=f"lnk_lbl_{idx}",
            placeholder="e.g. Product Catalog PDF",
        )
      with col_url:
        url = st.text_input(
            f"URL #{idx+1}",
            value=link_item["url"],
            key=f"lnk_url_{idx}",
            placeholder="https://...",
        )
      with col_del:
        st.write("")
        st.write("")
        if st.button("❌", key=f"del_lnk_{idx}"):
          st.session_state.custom_links.pop(idx)
          st.rerun()
      updated_links.append({"label": lbl, "url": url})

    st.session_state.custom_links = updated_links

    if st.button("➕ Add Link"):
      st.session_state.custom_links.append({"label": "", "url": ""})
      st.rerun()

    st.markdown("---")
    video_attachments = st.file_uploader(
        "Attach Videos",
        type=["mp4", "mov", "avi"],
        accept_multiple_files=True,
        key="vid_att1",
    )
    attachments = st.file_uploader(
        "Attach Documents / Catalogs (PDF/XLS)",
        accept_multiple_files=True,
        key="att1",
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

  valid_accounts = [
      acc
      for acc in st.session_state.sender_accounts
      if acc["email"].strip() and acc["password"].strip()
  ]

  if start_clicked:
    if not uploaded_file:
      st.error("Please upload an Excel or CSV file first.")
    elif not valid_accounts:
      st.error(
          "Please enter at least one Sender Email and App Password in the"
          " sidebar."
      )
    elif df_clean.empty:
      st.error("No valid recipient records found.")
    elif not active_subjects:
      st.error("Please provide at least one subject line variation.")
    else:
      st.session_state.is_sending = True
      server = None
      current_acc_idx = 0
      current_acc_sent_count = 0

      try:
        active_account = valid_accounts[current_acc_idx]
        server = connect_smtp_account(active_account)

        progress = st.progress(0)
        status_msg = st.empty()
        total = len(df_clean)
        min_sec, max_sec = delay_range

        for i, (_, row) in enumerate(df_clean.iterrows()):
          if not st.session_state.is_sending:
            status_msg.warning("Interrupted by user.")
            break

          if (
              current_acc_sent_count >= emails_per_account_limit
              and len(valid_accounts) > 1
          ):
            try:
              server.quit()
            except Exception:
              pass

            current_acc_idx = (current_acc_idx + 1) % len(valid_accounts)
            current_acc_sent_count = 0
            active_account = valid_accounts[current_acc_idx]

            status_msg.info(
                f"🔄 Rotated sender account to: {active_account['email']}"
            )
            server = connect_smtp_account(active_account)

          active_sender_email = active_account["email"].strip()

          row_dict = row.to_dict()
          row_dict["sender_name"] = sender_name

          recipient_email = str(row_dict.get("Email", "")).strip()
          recipient_name = (
              str(row_dict.get("Name", "")).strip() or "Valued Partner"
          )
          recipient_comp = (
              str(row_dict.get("Company", "")).strip() or "your organization"
          )
          recipient_desig = (
              str(row_dict.get("Designation", "")).strip()
              or "Purchase Manager"
          )
          recipient_dept = (
              str(row_dict.get("Department", "")).strip()
              or "Procurement & Maintenance"
          )
          recipient_city = (
              str(row_dict.get("City", "")).strip() or "your area"
          )

          chosen_sub_template = random.choice(active_subjects)

          rendered_body = render_template(body_val, row_dict)
          rendered_subject = render_template(chosen_sub_template, row_dict)

          html_body = build_html_body(
              rendered_body,
              st.session_state.custom_links,
              [v.name for v in video_attachments]
              if video_attachments
              else [],
          )

          msg = MIMEMultipart("mixed")
          msg["From"] = f"{sender_name} <{active_sender_email}>"
          msg["To"] = recipient_email
          if cc_email.strip():
            msg["Cc"] = cc_email.strip()

          msg["Subject"] = Header(rendered_subject, "utf-8").encode()

          msg_alt = MIMEMultipart("alternative")
          msg_alt.attach(MIMEText(html_body, "html", "utf-8"))
          msg.attach(msg_alt)

          if attachments:
            for att in attachments:
              part = MIMEBase("application", "octet-stream")
              part.set_payload(att.getvalue())
              encoders.encode_base64(part)
              part.add_header(
                  "Content-Disposition", f'attachment; filename="{att.name}"'
              )
              msg.attach(part)

          if video_attachments:
            for vid in video_attachments:
              vid_part = MIMEBase("video", "octet-stream")
              vid_part.set_payload(vid.getvalue())
              encoders.encode_base64(vid_part)
              vid_part.add_header(
                  "Content-Disposition", f'attachment; filename="{vid.name}"'
              )
              msg.attach(vid_part)

          recipients_list = [recipient_email]
          if cc_email.strip():
            recipients_list.append(cc_email.strip())

          server.sendmail(active_sender_email, recipients_list, msg.as_string())
          log_initial_email(
              recipient_name,
              recipient_email,
              recipient_comp,
              recipient_desig,
              recipient_dept,
              recipient_city,
          )

          current_acc_sent_count += 1
          actual_delay = round(random.uniform(min_sec, max_sec), 1)

          status_msg.text(
              f"[{i+1}/{total}] Sent from ({active_sender_email}) to:"
              f" {recipient_email} ({recipient_comp}) | Waiting {actual_delay}s"
          )
          progress.progress((i + 1) / total)
          time.sleep(actual_delay)

        if st.session_state.is_sending:
          st.success("🎉 Outreach campaign completed successfully!")
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
      if not valid_accounts:
        st.error("Provide credentials in sidebar.")
      else:
        first_acc = valid_accounts[0]
        senders, err = check_inbox_for_replies(
            first_acc["email"], first_acc["password"], imap_server
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
        value="Following up: Bearings & industrial supply partnership",
        key="main_fs1",
    )
    fu_body = st.text_area(
        "Follow-Up Body Template",
        value="""Dear {Name},

I am following up on my previous message regarding industrial bearing and component supply for {Company}. 

We carry ready stock for EVO, Renold, Max Spares, KYK Japan, Toyo Power, and JK Fenner products.

If your procurement or maintenance team is currently reviewing bearing vendor lists or requires pricing for an upcoming RFQ/purchase order, please let me know. Happy to share our rate card or schedule a 5-minute call.

Warm regards,
{sender_name}
India Bearings & Mill Stores | EVO Bearings and Machineries Pvt. Ltd.
+91 9831356591 | +91 9088363391""",
        height=200,
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
      current_acc_idx = 0
      current_acc_sent_count = 0

      try:
        active_account = valid_accounts[current_acc_idx]
        server = connect_smtp_account(active_account)

        progress_fu = st.progress(0)
        status_fu = st.empty()
        total_fu = len(candidates)

        for i, (_, row) in enumerate(candidates.iterrows()):
          if (
              current_acc_sent_count >= emails_per_account_limit
              and len(valid_accounts) > 1
          ):
            try:
              server.quit()
            except Exception:
              pass

            current_acc_idx = (current_acc_idx + 1) % len(valid_accounts)
            current_acc_sent_count = 0
            active_account = valid_accounts[current_acc_idx]
            server = connect_smtp_account(active_account)

          active_sender_email = active_account["email"].strip()
          rec = str(row["email"]).strip()

          row_dict = row.to_dict()
          row_dict["sender_name"] = sender_name

          rendered_fu_body = render_template(fu_body, row_dict)
          rendered_fu_subject = render_template(fu_subject, row_dict)

          html_b = build_html_body(
              rendered_fu_body, custom_links=st.session_state.custom_links
          )

          msg = MIMEMultipart("alternative")
          msg["From"] = f"{sender_name} <{active_sender_email}>"
          msg["To"] = rec
          if cc_email.strip():
            msg["Cc"] = cc_email.strip()

          msg["Subject"] = Header(rendered_fu_subject, "utf-8").encode()
          msg.attach(MIMEText(html_b, "html", "utf-8"))

          recipients_list = [rec]
          if cc_email.strip():
            recipients_list.append(cc_email.strip())

          server.sendmail(active_sender_email, recipients_list, msg.as_string())
          log_followup_email(rec)

          current_acc_sent_count += 1
          actual_delay = round(
              random.uniform(fu_delay_range[0], fu_delay_range[1]), 1
          )
          status_fu.text(
              f"[{i+1}/{total_fu}] Follow-up sent from ({active_sender_email})"
              f" to: {rec} (Waiting {actual_delay}s)"
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
  if DB_URL:
    conn = get_db_connection()
    try:
      all_logs = pd.read_sql_query(
          "SELECT id, name, company, designation, department, city, email,"
          " initial_sent_date, status, followup_sent_date FROM sent_emails ORDER"
          " BY id DESC",
          conn,
      )
      st.dataframe(all_logs)
    except Exception as e:
      st.error(f"Could not load database records: {e}")
    finally:
      conn.close()
  else:
    st.warning("Database URL is not configured.")
