import csv
import datetime
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
# 1. DATABASE SETUP WITH TIMEOUT PROTECTION
# ==============================================================================
DB_FILE = "email_automation.db"


def init_db():
  """Initialize SQLite table with proper timeout to handle multi-thread writing."""
  conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS email_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            university TEXT,
            email TEXT,
            subject TEXT,
            status TEXT,
            timestamp TEXT
        )
    """)
  conn.commit()
  conn.close()


init_db()


def log_send_status(name, university, email, subject, status):
  """Save log entry safely with busy timeout setting."""
  try:
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    cursor = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute(
        """
            INSERT INTO email_logs (name, university, email, subject, status, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(name),
            str(university),
            str(email),
            str(subject),
            str(status),
            now_str,
        ),
    )
    conn.commit()
    conn.close()
  except Exception as e:
    print(f"Logging Error: {e}")


# ==============================================================================
# 2. EMAIL BUILDER
# ==============================================================================
def create_proposal_email(
    sender_email,
    to_email,
    cc_email,
    name,
    university,
    department,
    body_template,
    attachment_bytes,
    attachment_filename,
):
  msg = EmailMessage()
  msg["From"] = f"EVO Network Bharat <{sender_email}>"
  msg["To"] = to_email
  if cc_email:
    msg["Cc"] = cc_email

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

  formatted_body = body_template.format(
      name=name, university=university, department=department
  )
  msg.set_content(formatted_body)

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
# 3. CONTROLLED BACKGROUND WORKER
# ==============================================================================
def run_outreach_worker(
    stop_event,
    df,
    smtp_server,
    smtp_port,
    sender_email,
    sender_password,
    cc_email,
    body_template,
    attachment_bytes,
    attachment_filename,
    min_delay,
    max_delay,
):
  """Executes outreach loop while constantly monitoring the stop signal."""
  try:
    with smtplib.SMTP_SSL(smtp_server, int(smtp_port), timeout=15) as server:
      server.login(sender_email, sender_password)

      for count, row in df.iterrows():
        # Check if user clicked "Stop Campaign"
        if stop_event.is_set():
          log_send_status(
              "SYSTEM",
              "N/A",
              "N/A",
              "N/A",
              "CAMPAIGN MANUALLY STOPPED BY USER",
          )
          break

        recipient_email = str(row.get("Email", "")).strip()
        name = str(row.get("Name", "")).strip()
        univ = str(row.get("University", "")).strip()
        dept = str(row.get("Department", "")).strip()

        if not recipient_email or recipient_email.lower() == "nan":
          log_send_status(
              name, univ, "N/A", "N/A", "SKIPPED (Missing Email Address)"
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
              attachment_bytes,
              attachment_filename,
          )

          server.send_message(email_msg)
          log_send_status(name, univ, recipient_email, chosen_subject, "SENT")

        except Exception as e:
          log_send_status(
              name, univ, recipient_email, "N/A", f"FAILED ({str(e)})"
          )

        # Dynamic delay checkable against stop signal
        wait_time = random.randint(min_delay, max_delay)
        for _ in range(wait_time):
          if stop_event.is_set():
            break
          time.sleep(1)

  except Exception as e:
    log_send_status(
        "SYSTEM", "N/A", "N/A", "N/A", f"CRITICAL SMTP ERROR ({str(e)})"
    )
  finally:
    st.session_state["campaign_running"] = False


# ==============================================================================
# 4. STREAMLIT INTERFACE WITH STATE MANAGEMENT
# ==============================================================================
st.set_page_config(
    page_title="Quantum Outreach Automation", page_icon="⚛️", layout="wide"
)

# Initialize Session States
if "campaign_running" not in st.session_state:
  st.session_state["campaign_running"] = False
if "stop_event" not in st.session_state:
  st.session_state["stop_event"] = threading.Event()

st.title("⚛️ EVO Network Outreach Automation")
st.caption(
    "Automated academic proposal campaigns with active state management and"
    " thread safety."
)

# Sidebar Configuration
with st.sidebar:
  st.header("⚙️ SMTP Settings")
  smtp_server = st.text_input("SMTP Server", "smtp.gmail.com")
  smtp_port = st.number_input("SMTP Port", value=465)
  sender_email = st.text_input("Sender Email", value="evoalpha30@gmail.com")
  sender_password = st.text_input("Google App Password", type="password")
  cc_email = st.text_input(
      "CC Email", value="lakshyarajdevgurujaiswal@gmail.com"
  )

  st.divider()
  st.header("🛡️ Sending Delays")
  min_delay = st.number_input("Min Delay (Seconds)", value=30)
  max_delay = st.number_input("Max Delay (Seconds)", value=60)

col1, col2 = st.columns(2)
with col1:
  csv_file = st.file_uploader("Upload Recipients (.csv)", type=["csv"])
with col2:
  attachment_file = st.file_uploader("Upload Proposal PDF", type=["pdf"])

default_body = """Dear {name},

I hope this email finds you well.

As quantum technologies move from theoretical physics into mainstream software engineering, biotechnology, and chemistry, leading global universities are racing to prepare their students for the quantum workforce. However, setting up physical labs or command-line coding kits often introduces major friction, licensing costs, and steep learning curves.

We would like to introduce Alpha ParadoxQC—the world’s first integrated, browser-based Quantum Computing Education and Research Platform.

Our platform completely eliminates setup hurdles by providing:
• Interactive Quantum Circuit Builder: Visual multi-qubit design, local simulations, and direct QPU execution to real quantum machines (IonQ, Rigetti, IQM).
• Quantum Chemistry (VQE) Simulator: Visualizing ground-state energies for 30+ molecules and a Custom Molecule Inventor for student research.
• Pharma & Drug Discovery Module: Live visual docking simulators (like COX-2 COX inhibitors), automated ADMET profiling, and Lipinski Rule-of-Five checks.

Our Proposal to {university}:
We are currently selecting forward-thinking Indian universities to receive a Free, Custom Live Demonstration of the platform for your science and engineering faculty. Following this interactive demo, we can set up a structured pilot program, paving the way for an Annual Rate Contract (ARC) to equip your entire student body with personal, cloud-based quantum sandboxes.

EVO Network Bharat Private Limited (an initiative by Lakshya Raj Devguru Jaiswal) is the official implementation and marketing partner for this academic roll-out.

We would love to discuss how we can deploy this trial for your departments and explore a long-term partnership. Please let us know when we can connect for a detailed discussion, which we can hold virtually or in person at your campus.

Please feel free to reach back directly at the coordinates listed below.

Sincerely,

EVO Network Bharat Private Limited 
Phone: +91 6293755931 / +91 9831356591
Email: evoalpha30@gmail.com
Website: www.alphaparadoxqc.com"""

body_template = st.text_area("Email Template Body", value=default_body, height=250)

if csv_file is not None:
  df = pd.read_csv(csv_file)
  st.subheader("📋 Recipient List Preview")
  st.dataframe(df.head(5), use_container_width=True)

  # Control Dashboard Buttons
  btn_col1, btn_col2 = st.columns(2)

  with btn_col1:
    if st.button(
        "🚀 Launch Campaign", disabled=st.session_state["campaign_running"]
    ):
      if not sender_password:
        st.error("Please enter your 16-character App Password in the sidebar!")
      elif attachment_file is None:
        st.warning("Please upload the proposal PDF file before launching.")
      else:
        # Safely extract bytes using getvalue() to avoid stream EOF issues
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
                att_bytes,
                att_name,
                min_delay,
                max_delay,
            ),
            daemon=True,
        )
        worker_thread.start()
        st.success("Campaign launched! Monitoring active thread.")
        st.rerun()

  with btn_col2:
    if st.button("⏹️ Stop Campaign", disabled=not st.session_state['campaign_running']):
      st.session_state["stop_event"].set()
      st.session_state["campaign_running"] = False
      st.warning("Stop signal sent to active worker thread.")
      st.rerun()

# Display Current Campaign Status
if st.session_state["campaign_running"]:
  st.info(
      "🟢 **Status:** Campaign is currently running in the background..."
  )
else:
  st.caption("⚪ **Status:** Idle")

st.divider()
st.subheader("📊 Live Activity Logs")
if st.button("🔄 Refresh Logs"):
  st.rerun()

conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
logs_df = pd.read_sql_query(
    "SELECT * FROM email_logs ORDER BY id DESC LIMIT 50", conn
)
conn.close()

if not logs_df.empty:
  st.dataframe(logs_df, use_container_width=True)
else:
  st.info("No outreach logs recorded yet.")
