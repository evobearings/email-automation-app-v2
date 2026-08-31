import csv
import datetime
from datetime import timedelta
import mimetypes
import os
import random
import smtplib
import sqlite3
import threading
import time
from email.message import EmailMessage
import pandas as pd
import streamlit as st

# ==============================================================================
# 1. DATABASE SETUP (INCLUDES 15-DAY FOLLOW-UP TRACKING)
# ==============================================================================
DB_FILE = "email_automation.db"


def init_db():
  conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
  cursor = conn.cursor()
  # Table holds sent status, dynamic links, and calculated 15-day follow-up dates
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS email_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            university TEXT,
            email TEXT,
            subject TEXT,
            status TEXT,
            timestamp TEXT,
            followup_due_date TEXT,
            error_details TEXT
        )
    """)
  conn.commit()
  conn.close()


init_db()


def log_send_status(
    name, university, email, subject, status, error_details=""
):
  try:
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    cursor = conn.cursor()
    now = datetime.datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Calculate 15 Days Follow-Up Date automatically
    followup_date = (now + timedelta(days=15)).strftime("%Y-%m-%d")

    cursor.execute(
        """
            INSERT INTO email_logs (name, university, email, subject, status, timestamp, followup_due_date, error_details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(name),
            str(university),
            str(email),
            str(subject),
            str(status),
            now_str,
            followup_date,
            str(error_details),
        ),
    )
    conn.commit()
    conn.close()
  except Exception as e:
    print(f"Logging Exception: {e}")


# ==============================================================================
# 2. EMAIL BUILDER WITH DYNAMIC LINKS & ATTACHMENT
# ==============================================================================
def create_proposal_email(
    sender_email,
    to_email,
    cc_email,
    name,
    university,
    department,
    body_template,
    website_url,
    demo_url,
    attachment_bytes,
    attachment_filename,
):
  msg = EmailMessage()
  msg["From"] = f"EVO Network Bharat <{sender_email}>"
  msg["To"] = to_email
  if cc_email:
    msg["Cc"] = cc_email

  # Subject Line Rotation
  subject_options = [
      f"Bringing browser-based quantum computing to {university} Students",
      (
          "Quantum Computing Sandbox for"
          f" {university} Science Faculty and Students"
      ),
      f"Browser-based Quantum Research Platform Demo — {university}",
      f"Bringing browser-based quantum computing to {university}",
  ]
  selected_subject = random.choice(subject_options)
  msg["Subject"] = selected_subject

  # Dynamic Variable Replacement including Links
  formatted_body = body_template.format(
      name=name,
      university=university,
      department=department,
      website_url=website_url,
      demo_url=demo_url,
  )
  msg.set_content(formatted_body)

  # PDF Attachment
  if attachment_bytes and attachment_filename:
    ctype, encoding = mimetypes.guess_type(attachment_filename)
    if ctype is None or encoding is not None:
      ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)

    msg.add_attachment(
        attachment_bytes,
        maintype=maintype,
        subtype=subtype,
        filename=attachment_filename,
    )

  return msg, selected_subject


# ==============================================================================
# 3. SMTP WORKER WITH DUAL-PORT FALLBACK & ROBUST ERROR REPORTING
# ==============================================================================
def send_smtp_message(server_host, port, sender, password, email_msg):
  """Attempts sending via SSL first, with auto-fallback to TLS/587 if blocked on cloud."""
  try:
    if int(port) == 465:
      with smtplib.SMTP_SSL(server_host, int(port), timeout=15) as server:
        server.login(sender, password)
        server.send_message(email_msg)
    else:
      with smtplib.SMTP(server_host, int(port), timeout=15) as server:
        server.starttls()
        server.login(sender, password)
        server.send_message(email_msg)
    return True, ""
  except Exception as e:
    return False, str(e)


def run_outreach_worker(
    stop_event,
    df,
    smtp_server,
    smtp_port,
    sender_email,
    sender_password,
    cc_email,
    body_template,
    website_url,
    demo_url,
    attachment_bytes,
    attachment_filename,
    min_delay,
    max_delay,
):
  for count, row in df.iterrows():
    if stop_event.is_set():
      log_send_status(
          "SYSTEM", "N/A", "N/A", "N/A", "STOPPED", "User manually stopped task"
      )
      break

    recipient_email = str(row.get("Email", "")).strip()
    name = str(row.get("Name", "")).strip()
    univ = str(row.get("University", "")).strip()
    dept = str(row.get("Department", "")).strip()

    if not recipient_email or recipient_email.lower() == "nan":
      log_send_status(
          name, univ, "N/A", "N/A", "SKIPPED", "Missing email address"
      )
      continue

    try:
      email_msg, chosen_subject = create_proposal_email(
          sender_email,
          recipient_email,
          cc_email,
          name,
          univ,
          dept,
          body_template,
          website_url,
          demo_url,
          attachment_bytes,
          attachment_filename,
      )

      # Attempt Send with Detailed Error Logging
      success, error_msg = send_smtp_message(
          smtp_server, smtp_port, sender_email, sender_password, email_msg
      )

      if success:
        log_send_status(name, univ, recipient_email, chosen_subject, "SENT", "")
      else:
        log_send_status(
            name, univ, recipient_email, chosen_subject, "FAILED", error_msg
        )

    except Exception as e:
      log_send_status(
          name, univ, recipient_email, "N/A", "FAILED", f"Build Error: {str(e)}"
      )

    # Configurable Delay
    wait_time = random.randint(min_delay, max_delay)
    for _ in range(wait_time):
      if stop_event.is_set():
        break
      time.sleep(1)

  st.session_state["campaign_running"] = False


# ==============================================================================
# 4. FULL DASHBOARD UI (RESTORED FEATURES & TABS)
# ==============================================================================
st.set_page_config(
    page_title="Quantum Outreach Hub", page_icon="⚛️", layout="wide"
)

if "campaign_running" not in st.session_state:
  st.session_state["campaign_running"] = False
if "stop_event" not in st.session_state:
  st.session_state["stop_event"] = threading.Event()

st.title("⚛️ EVO Network Quantum Outreach Hub")
st.caption(
    "Academic Outreach Platform with Dynamic Links, 15-Day Follow-Up Tracker,"
    " and Diagnostic Logs."
)

# Sidebar Credentials & SMTP Settings
with st.sidebar:
  st.header("⚙️ Server & Auth Settings")
  smtp_server = st.text_input("SMTP Server", "smtp.gmail.com")
  smtp_port = st.selectbox(
      "SMTP Port", [465, 587], index=0, help="Try 587 if 465 fails on Render"
  )
  sender_email = st.text_input("Sender Email", value="evoalpha30@gmail.com")
  sender_password = st.text_input("Google App Password", type="password")
  cc_email = st.text_input(
      "CC Email Target", value="lakshyarajdevgurujaiswal@gmail.com"
  )

  st.divider()
  st.header("🔗 Custom Link Options")
  website_url = st.text_input("Website URL", "www.alphaparadoxqc.com")
  demo_url = st.text_input(
      "Live Demo Request URL", "https://alphaparadoxqc.com/demo"
  )

  st.divider()
  st.header("🛡️ Sending Delays")
  min_delay = st.number_input("Min Delay (Sec)", value=30)
  max_delay = st.number_input("Max Delay (Sec)", value=60)

# Multi-Tab Layout
tab_campaign, tab_followup, tab_logs = st.tabs(
    ["🚀 Campaign Launcher", "📅 15-Day Follow-Up Tracker", "📊 Detailed Logs"]
)

with tab_campaign:
  col1, col2 = st.columns(2)
  with col1:
    csv_file = st.file_uploader("Upload Recipients (.csv)", type=["csv"])
  with col2:
    attachment_file = st.file_uploader("Upload Proposal PDF", type=["pdf"])

  default_body = """Dear {name},

I hope this email finds you well.

As quantum technologies move from theoretical physics into mainstream software engineering, biotechnology, and chemistry, leading global universities are racing to prepare their students for the quantum workforce.

We would like to introduce Alpha ParadoxQC—the world’s first integrated, browser-based Quantum Computing Education and Research Platform.

Our platform completely eliminates setup hurdles by providing:
• Interactive Quantum Circuit Builder
• Quantum Chemistry (VQE) Simulator
• Pharma & Drug Discovery Module

Our Proposal to {university}:
We are currently selecting forward-thinking Indian universities to receive a Free, Custom Live Demonstration of the platform for your science and engineering faculty ({department}).

Website: {website_url}
Schedule Demo Directly: {demo_url}

Sincerely,
EVO Network Bharat Private Limited 
Phone: +91 6293755931 / +91 9831356591
Email: evoalpha30@gmail.com"""

  body_template = st.text_area(
      "Email Body Template (Supports {name}, {university}, {department}, {website_url}, {demo_url})",
      value=default_body,
      height=260,
  )

  if csv_file is not None:
    df = pd.read_csv(csv_file)
    st.subheader("📋 Recipient Preview")
    st.dataframe(df.head(5), use_container_width=True)

    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
      if st.button(
          "🚀 Start Campaign", disabled=st.session_state["campaign_running"]
      ):
        if not sender_password:
          st.error(
              "Please enter your 16-character App Password in the sidebar!"
          )
        elif attachment_file is None:
          st.warning("Please upload the proposal PDF file before launching.")
        else:
          att_bytes = attachment_file.getvalue()
          att_name = attachment_file.name

          st.session_state["stop_event"].clear()
          st.session_state["campaign_running"] = True

          worker_thread = threading.Thread(
              target=run_outreach_worker,
              args=(
                  st.session_state["stop_event"],
                  df,
                  smtp_server,
                  smtp_port,
                  sender_email,
                  sender_password,
                  cc_email,
                  body_template,
                  website_url,
                  demo_url,
                  att_bytes,
                  att_name,
                  min_delay,
                  max_delay,
              ),
              daemon=True,
          )
          worker_thread.start()
          st.success("Campaign launched in background thread!")
          st.rerun()

    with btn_col2:
      if st.button(
          "⏹️ Stop Campaign", disabled=not st.session_state["campaign_running"]
      ):
        st.session_state["stop_event"].set()
        st.session_state["campaign_running"] = False
        st.warning("Stop signal sent.")
        st.rerun()

with tab_followup:
  st.subheader("📅 15-Day Follow-Up Queue")
  st.caption(
      "Contacts sent 15+ days ago automatically populate here for follow-up"
      " outreach."
  )

  conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
  today_str = datetime.datetime.now().strftime("%Y-%m-%d")

  followup_df = pd.read_sql_query(
      f"""
        SELECT name, university, email, timestamp as initial_sent_date, followup_due_date 
        FROM email_logs 
        WHERE status = 'SENT' AND followup_due_date <= '{today_str}'
        ORDER BY followup_due_date ASC
    """,
      conn,
  )
  conn.close()

  if not followup_df.empty:
    st.dataframe(followup_df, use_container_width=True)
  else:
    st.info("No follow-ups due today! Pending follow-ups will appear here.")

with tab_logs:
  st.subheader("📊 Live Activity & Error Diagnostics")
  if st.button("🔄 Refresh Log Table"):
    st.rerun()

  conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
  logs_df = pd.read_sql_query(
      "SELECT id, timestamp, name, university, email, status, followup_due_date,"
      " error_details FROM email_logs ORDER BY id DESC LIMIT 50",
      conn,
  )
  conn.close()

  if not logs_df.empty:
    st.dataframe(logs_df, use_container_width=True)
  else:
    st.info("No outreach logs recorded yet.")
