"""
Daily log-ingestion VARIANCE check (accounts + apps from config.json).

For every account in config["accounts"] and every app in config["apps"]:
    current  = log GB ingested in the last 24 hours
    baseline = daily average of the 7 days before that (192h ago -> 24h ago, / 7)
    variance = (current - baseline) / baseline * 100
Alerts (email with remediation link) when variance > thresholdPct (default 20%).
Increase only. Daily budget fields in config.json are NOT used.

Optional config.json block:
    "varianceCheck": { "thresholdPct": 20, "minCurrentGB": 0.01 }

Run:
    python job_24hr_critical_dynamic_check.py --run-now
    python job_24hr_critical_dynamic_check.py            # daily at schedules.criticalJob24hrTimeIST
"""
import argparse
import time
from datetime import datetime
from html import escape
from urllib.parse import urlencode

import schedule

from nr_utils import load_config, get_ingestion_variance, send_html_email


def _fmt_pct(v):
    return "n/a (no baseline)" if v is None else f"{v:+.2f}%"


def _dedupe_accounts(accounts):
    """config.json may list the same accountId twice - merge so we alert once, to all recipients."""
    merged = {}
    for acc in accounts:
        acc_id = str(acc["accountId"])
        entry = merged.setdefault(acc_id, {"accountId": acc_id, "emails": []})
        for e in acc.get("accountAlertEmailIds", []):
            if e not in entry["emails"]:
                entry["emails"].append(e)
    return list(merged.values())


def _build_email(scope_label, r, action_url):
    is_new = r["is_new_source"]
    title = "NEW LOG SOURCE DETECTED" if is_new else "LOG INGESTION SPIKE vs 7-DAY AVERAGE"
    pct_color = "red" if (r["variance_pct"] or 0) > 0 else "green"
    reason = (
        "No ingestion in the previous 7 days, but logs are now arriving."
        if is_new else
        f"Last-24h ingestion is {r['variance_pct']:.2f}% above the 7-day daily average "
        f"(threshold {r['threshold_pct']:.0f}%)."
    )
    return f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333;">
        <h2 style="color: #d9534f;">🚨 {title}</h2>
        <p><strong>{escape(scope_label)}</strong></p>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse;">
          <tr><td><strong>Account ID</strong></td><td>{escape(r['account_id'])}</td></tr>
          {f"<tr><td><strong>App Name</strong></td><td>{escape(r['app_name'])}</td></tr>" if r['app_name'] else ""}
          <tr><td><strong>Last 24h Ingestion</strong></td><td>{r['current_24h_gb']:.4f} GB</td></tr>
          <tr><td><strong>7-Day Daily Average</strong></td><td>{r['baseline_daily_avg_gb']:.4f} GB</td></tr>
          <tr><td><strong>7-Day Total (baseline window)</strong></td><td>{r['baseline_7d_total_gb']:.4f} GB</td></tr>
          <tr><td><strong>Variance</strong></td>
              <td style="color: {pct_color}; font-weight: bold;">{_fmt_pct(r['variance_pct'])}</td></tr>
          <tr><td><strong>Alert Threshold</strong></td><td>&gt; {r['threshold_pct']:.0f}% increase</td></tr>
        </table>
        <p style="color: red; font-weight: bold;">⚠️ {escape(reason)}</p>
        <a href="{escape(action_url)}"
           style="background-color: #d9534f; color: white; padding: 12px 20px; text-decoration: none;
                  border-radius: 4px; display: inline-block; font-weight: bold;">
           Invoke AI Workflow &amp; Remediate via Drop Rules
        </a>
      </body>
    </html>
    """


def _print_result(label, r):
    status = "🚨 ALERT" if r["is_spike"] else "✅ OK"
    if r.get("is_new_source"):
        status += " (new log source - no ingestion in previous 7 days)"
    elif not r["is_spike"] and (r["variance_pct"] or 0) > r["threshold_pct"]:
        status += " (above threshold but volume below minCurrentGB - ignored as noise)"
    print(f"  {label}")
    print(f"    ├─ Last 24h          : {r['current_24h_gb']:.4f} GB")
    print(f"    ├─ 7-day daily avg   : {r['baseline_daily_avg_gb']:.4f} GB")
    print(f"    ├─ Variance          : {_fmt_pct(r['variance_pct'])}  (threshold > {r['threshold_pct']:.0f}%)")
    print(f"    └─ Result            : {status}")


def run_24hr_check(config_file):
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🚀 Log ingestion variance check "
          f"(last 24h vs 7-day daily average)\n")
    config = load_config(config_file)
    base_url = config.get("webhook", {}).get("baseUrl", "http://localhost:5000").rstrip("/")
    vc = config.get("varianceCheck", {})
    threshold = float(vc.get("thresholdPct", 20.0))
    min_gb = float(vc.get("minCurrentGB", 0.01))

    summary = {"checked": 0, "alerts": 0, "errors": 0}

    # ---------------- 1. ACCOUNT LEVEL ----------------
    print("📊 ACCOUNT LEVEL")
    for acc in _dedupe_accounts(config.get("accounts", [])):
        acc_id = acc["accountId"]
        label = f"Account {acc_id}"
        summary["checked"] += 1
        try:
            r = get_ingestion_variance(acc_id, threshold_pct=threshold, min_current_gb=min_gb,
                                       config_file=config_file)
        except Exception as e:
            summary["errors"] += 1
            print(f"  {label}: ❌ query failed: {e}\n")
            continue
        _print_result(label, r)

        if r["is_spike"]:
            url = f"{base_url}/drop-logs?" + urlencode(
                {"target_type": "account", "account_id": acc_id, "config": config_file})
            subject = (f"NEW LOG SOURCE: Account {acc_id}" if r["is_new_source"] else
                       f"LOG SPIKE {_fmt_pct(r['variance_pct'])} vs 7-day avg: Account {acc_id}")
            try:
                send_html_email(subject, _build_email(label, r, url), acc["emails"], config_file=config_file)
                summary["alerts"] += 1
                print(f"    └─ Alert emailed to {acc['emails']}")
            except Exception as e:
                summary["errors"] += 1
                print(f"    └─ ❌ Email failed: {e}")
        print()

    # ---------------- 2. APP LEVEL ----------------
    print("📦 APP LEVEL")
    for app in config.get("apps", []):
        app_name = app["appName"]
        acc_id = str(app["accountId"])
        label = f"App {app_name} (account {acc_id})"
        summary["checked"] += 1
        try:
            r = get_ingestion_variance(acc_id, app_name=app_name, threshold_pct=threshold,
                                       min_current_gb=min_gb, config_file=config_file)
        except Exception as e:
            summary["errors"] += 1
            print(f"  {label}: ❌ query failed: {e}\n")
            continue
        _print_result(label, r)

        if r["is_spike"]:
            url = f"{base_url}/drop-logs?" + urlencode(
                {"target_type": "app", "app_name": app_name, "account_id": acc_id, "config": config_file})
            subject = (f"NEW LOG SOURCE: {app_name}" if r["is_new_source"] else
                       f"LOG SPIKE {_fmt_pct(r['variance_pct'])} vs 7-day avg: {app_name}")
            emails = app.get("alertEmailIds", [])
            try:
                send_html_email(subject, _build_email(label, r, url), emails, config_file=config_file)
                summary["alerts"] += 1
                print(f"    └─ Alert emailed to {emails}")
            except Exception as e:
                summary["errors"] += 1
                print(f"    └─ ❌ Email failed: {e}")
        print()

    print(f"Done: {summary['checked']} checked, {summary['alerts']} alert(s) sent, {summary['errors']} error(s).")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily log ingestion variance check (24h vs 7-day avg)")
    parser.add_argument("--config", type=str, default="config.json", help="Path to config file.")
    parser.add_argument("--run-now", action="store_true", help="Execute immediately without waiting.")
    args = parser.parse_args()

    if args.run_now:
        run_24hr_check(config_file=args.config)
    else:
        config_data = load_config(args.config)
        scheduled_time = config_data.get("schedules", {}).get("criticalJob24hrTimeIST", "20:00")

        job = schedule.every().day.at(scheduled_time).do(run_24hr_check, config_file=args.config)

        print(f"Scheduling daily variance check at {scheduled_time}...")
        print(f"Current Local System Time : {datetime.now().strftime('%H:%M:%S')}")
        print(f"Next Scheduled Run Time   : {job.next_run}")

        while True:
            schedule.run_pending()
            time.sleep(2)
