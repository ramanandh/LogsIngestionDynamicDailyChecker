from html import escape
from flask import Flask, request, jsonify, render_template_string
from nr_utils import (
    load_config,
    get_log_ingestion_gb,
    get_account_total_log_ingestion_gb,
    create_log_drop_rule,
    create_account_log_drop_rule,
    estimate_drop_volume_gb,
    send_html_email,
    trigger_workflow_run,
    poll_workflow_status,
    build_ingestion_context
)

app = Flask(__name__)

PROGRESS_UI_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>New Relic AI Workflow Execution Console</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f6f9; margin: 30px; color: #333; }
        .container { max-width: 900px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        h1 { color: #008c99; font-size: 24px; border-bottom: 2px solid #eef2f5; padding-bottom: 10px; }
        .badge { background: #008c99; color: white; padding: 4px 10px; border-radius: 4px; font-size: 14px; font-weight: bold; }
        .run-badge { background: #5cb85c; color: white; padding: 4px 10px; border-radius: 4px; font-size: 13px; font-family: monospace; }

        /* Progress Bar & Spinner Styles */
        #progress-section { display: none; margin: 25px 0; background: #f0f7ff; border: 1px solid #008c99; padding: 20px; border-radius: 6px; }
        .progress-bar-container { width: 100%; background-color: #e0e0e0; border-radius: 20px; overflow: hidden; height: 22px; margin: 15px 0; }
        .progress-bar { width: 5%; height: 100%; background-color: #008c99; transition: width 0.4s ease; text-align: center; color: white; font-size: 12px; line-height: 22px; font-weight: bold; }
        .status-text { font-weight: bold; color: #008c99; margin-bottom: 5px; }

        .card { background: #fafafa; border: 1px solid #e0e0e0; padding: 20px; border-radius: 6px; margin-top: 20px; }
        .ai-response-box { background: #f0f7ff; border-left: 4px solid #008c99; padding: 20px; border-radius: 4px; margin: 20px 0; white-space: pre-wrap; font-size: 14px; line-height: 1.6; }
        label { font-weight: bold; display: block; margin-bottom: 8px; }
        input[type="number"], input[type="text"], select { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; margin-bottom: 15px; font-size: 15px; }
        .btn-wf { background-color: #008c99; color: white; padding: 12px 20px; border: none; border-radius: 4px; font-size: 16px; font-weight: bold; cursor: pointer; width: 100%; margin-bottom: 20px; }
        .btn-wf:hover { background-color: #006b75; }
        .btn-danger { background-color: #d9534f; color: white; padding: 12px 24px; border: none; border-radius: 4px; font-size: 16px; font-weight: bold; cursor: pointer; width: 100%; }
        .btn-danger:hover { background-color: #c9302c; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 New Relic AI Workflow: <code>logsIngestionAnalysis1</code></h1>
        <p><strong>Target Context:</strong> <span class="badge">Account {{ account_id }}</span></p>

        <!-- Trigger Button -->
        <button id="trigger-btn" class="btn-wf" onclick="startWorkflowExecution()">⚡ Execute Workflow & Fetch AI Recommendations</button>

        <!-- Progress Bar & Status Section -->
        <div id="progress-section">
            <div class="status-text" id="status-label">Initializing Workflow Call...</div>
            <div class="progress-bar-container">
                <div class="progress-bar" id="progress-bar">5%</div>
            </div>
            <p id="sub-status" style="margin: 0; font-size: 13px; color: #666;">Contacting New Relic NerdGraph API...</p>
        </div>

        <!-- AI Output & Pipeline Rule Section -->
        <div id="results-section" style="display: none;">
            <div style="background: #eef2f5; padding: 12px; border-radius: 5px; margin-bottom: 15px;">
                <p style="margin: 0;"><strong>Workflow Run ID:</strong> <span class="run-badge" id="display-run-id"></span></p>
            </div>

            <h3>🤖 AI Agent Analysis & Recommendations</h3>
            <div class="ai-response-box" id="ai-response-content"></div>

            <div class="card">
                <h3>⚙️ Configure Recommended Pipeline Control Drop Rule</h3>
                <form action="/drop-logs" method="POST">
                    <input type="hidden" name="target_type" value="{{ target_type }}">
                    <input type="hidden" name="app_name" value="{{ app_name }}">
                    <input type="hidden" name="account_id" value="{{ account_id }}">
                    <input type="hidden" name="config" value="{{ config_file }}">

                    <label for="rule_select">Select Action Strategy:</label>
                    <select id="rule_select" onchange="applyRuleSelection(this)">
                        <option value="SELECT * FROM Log WHERE level IN ('DEBUG', 'DEBUGGING', 'TRACE')">Drop DEBUG & TRACE Level Logs (~48% estimated reduction)</option>
                        <option value="SELECT * FROM Log WHERE message LIKE '%GET /health%' OR message LIKE '%GET /ready%'">Drop Health Probe Traffic Logs (~15% estimated reduction)</option>
                        <option value="SELECT * FROM Log WHERE message LIKE '%NullPointerException%'">Drop Duplicate Exception Stack Traces (~12% estimated reduction)</option>
                        <option value="CUSTOM">Custom NRQL Filter...</option>
                    </select>

                    <label for="custom_nrql">Target NRQL Drop Filter Condition:</label>
                    <input type="text" id="custom_nrql" name="custom_nrql" value="SELECT * FROM Log WHERE level IN ('DEBUG', 'DEBUGGING', 'TRACE')">

                    <label for="hours">Select Hours Back to Estimate Impact:</label>
                    <input type="number" id="hours" name="hours" value="4" min="1" max="72" required>

                    <button type="submit" class="btn-danger">Confirm & Configure Drop Rule in Pipeline Control</button>
                </form>
            </div>
        </div>
    </div>

    <script>
        var activeRunId = null;
        var progressPercent = 5;
        var pollInterval = null;

        function startWorkflowExecution() {
            document.getElementById('trigger-btn').style.display = 'none';
            document.getElementById('progress-section').style.display = 'block';
            document.getElementById('results-section').style.display = 'none';

            fetch('/api/start-workflow?account_id={{ account_id }}&config={{ config_file }}')
                .then(response => response.json())
                .then(data => {
                    if (data.error) {
                        alert('Workflow Error: ' + data.error);
                        resetUI();
                        return;
                    }
                    activeRunId = data.run_id;
                    updateProgress(25, "Workflow Triggered Successfully!", "Run ID: " + activeRunId + ". Invoking Ask AI Agent...");
                    pollInterval = setInterval(checkStatus, 3000);
                })
                .catch(err => {
                    alert('Failed to connect: ' + err);
                    resetUI();
                });
        }

        function checkStatus() {
            if (progressPercent < 85) {
                progressPercent += 10;
                updateProgress(progressPercent, "SRE AI Agent Investigating Logs...", "Analyzing ingestion patterns for Account {{ account_id }}...");
            }

            fetch('/api/workflow-status/' + activeRunId + '?account_id={{ account_id }}&config={{ config_file }}')
                .then(response => response.json())
                .then(data => {
                    if (data.status === 'RUNNING') {
                        document.getElementById('sub-status').innerText =
                            'NR run ' + (data.nr_run_id || '?') + ' running for ' + data.elapsed_seconds + 's...';
                    }
                    if (data.status === 'FAILED' || data.status === 'TIMEOUT') {
                        clearInterval(pollInterval);
                        updateProgress(100, 'Workflow ' + data.status, data.error || 'No result returned.');
                        document.getElementById('progress-bar').style.backgroundColor = '#d9534f';
                        return;
                    }
                    if (data.status === 'COMPLETED') {
                        clearInterval(pollInterval);
                        updateProgress(100, "Analysis Complete!", "Fetching final AI recommendations...");

                        setTimeout(() => {
                            document.getElementById('progress-section').style.display = 'none';
                            document.getElementById('results-section').style.display = 'block';
                            document.getElementById('display-run-id').innerText = activeRunId;
                            document.getElementById('ai-response-content').innerText = data.ai_response_text;
                        }, 800);
                    }
                });
        }

        function updateProgress(percent, statusMsg, subMsg) {
            var bar = document.getElementById('progress-bar');
            bar.style.width = percent + '%';
            bar.innerText = percent + '%';
            document.getElementById('status-label').innerText = statusMsg;
            document.getElementById('sub-status').innerText = subMsg;
        }

        function resetUI() {
            document.getElementById('trigger-btn').style.display = 'block';
            document.getElementById('progress-section').style.display = 'none';
        }

        function applyRuleSelection(selectEl) {
            var inputEl = document.getElementById('custom_nrql');
            if (selectEl.value !== 'CUSTOM') {
                inputEl.value = selectEl.value;
            } else {
                inputEl.value = 'SELECT * FROM Log WHERE ';
            }
        }
    </script>
</body>
</html>
"""


@app.route('/drop-logs', methods=['GET', 'POST'])
def drop_logs_handler():
    # 1. GET Request: Render progress console
    if request.method == 'GET':
        target_type = request.args.get('target_type', 'account')
        app_name = request.args.get('app_name', '')
        account_id = request.args.get('account_id', '8124546')
        config_file = request.args.get('config', 'config.json')

        return render_template_string(
            PROGRESS_UI_TEMPLATE,
            target_type=target_type,
            app_name=app_name,
            account_id=account_id,
            config_file=config_file
        )

    # 2. POST Request: Configure drop rule in New Relic
    elif request.method == 'POST':
        target_type = request.form.get('target_type')
        app_name = request.form.get('app_name')
        account_id = request.form.get('account_id', '8124546')
        config_file = request.form.get('config', 'config.json')
        hours = int(request.form.get('hours', 4))
        custom_nrql = request.form.get('custom_nrql', 'SELECT * FROM Log').strip()

        config = load_config(config_file)

        try:
            if target_type == "account":
                alert_emails = []
                for acc in config.get("accounts", []):
                    if str(acc["accountId"]) == str(account_id):
                        alert_emails = acc["accountAlertEmailIds"]
                        break

                original_24h_gb = get_account_total_log_ingestion_gb(hours_back=24, account_id=account_id,
                                                                     config_file=config_file)
                # GB the chosen filter matched in the last N hours, projected to a 24h day
                matched_gb = estimate_drop_volume_gb(custom_nrql, hours, account_id, config_file=config_file)
                dropped_hours_gb = matched_gb * (24.0 / hours)

                create_account_log_drop_rule(nrql_filter=custom_nrql, account_id=account_id,
                                             config_file=config_file)
                final_24h_gb = max(0.0, original_24h_gb - dropped_hours_gb)
                custom_nrql = escape(custom_nrql)

                subject = f"SUCCESS: Pipeline Drop Rule Applied to Account {account_id}"
                html_body = f"""
                <html>
                  <body style="font-family: Arial, sans-serif; color: #333;">
                    <h2 style="color: #5cb85c;">✅ PIPELINE CONTROL DROP RULE CONFIGURED</h2>
                    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse;">
                      <tr><td><strong>Account ID</strong></td><td>{account_id}</td></tr>
                      <tr><td><strong>NRQL Filter Applied</strong></td><td>{custom_nrql}</td></tr>
                      <tr><td><strong>Original 24h Volume</strong></td><td>{original_24h_gb:.4f} GB</td></tr>
                      <tr><td><strong>Est. Dropped per Day (from last {hours}h)</strong></td><td style="color: green; font-weight: bold;">{dropped_hours_gb:.4f} GB</td></tr>
                      <tr><td><strong>Effective Post-Drop Volume</strong></td><td>{final_24h_gb:.4f} GB</td></tr>
                    </table>
                  </body>
                </html>
                """
                send_html_email(subject, html_body, alert_emails, config_file=config_file)
                return f"<h1>Pipeline Drop Rule Configured for Account {account_id}!</h1><p>Filter: <code>{custom_nrql}</code>. Confirmation email sent.</p>"

            elif app_name:
                alert_emails = []
                for item in config.get("apps", []):
                    if item["appName"] == app_name and str(item.get("accountId")) == str(account_id):
                        alert_emails = item["alertEmailIds"]
                        break

                original_24h_gb = get_log_ingestion_gb(app_name, hours_back=24, account_id=account_id,
                                                       config_file=config_file)
                matched_gb = estimate_drop_volume_gb(custom_nrql, hours, account_id, app_name=app_name,
                                                     config_file=config_file)
                dropped_hours_gb = matched_gb * (24.0 / hours)

                create_log_drop_rule(app_name, nrql_filter=custom_nrql, account_id=account_id,
                                     config_file=config_file)
                final_24h_gb = max(0.0, original_24h_gb - dropped_hours_gb)
                custom_nrql, app_name = escape(custom_nrql), escape(app_name)

                subject = f"SUCCESS: Log Drop Applied for {app_name} ({hours}h Window)"
                html_body = f"""
                <html>
                  <body style="font-family: Arial, sans-serif; color: #333;">
                    <h2 style="color: #5cb85c;">✅ APP LOG DROP RULE APPLIED</h2>
                    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse;">
                      <tr><td><strong>App Name</strong></td><td>{app_name}</td></tr>
                      <tr><td><strong>Account ID</strong></td><td>{account_id}</td></tr>
                      <tr><td><strong>NRQL Filter Applied</strong></td><td>{custom_nrql}</td></tr>
                      <tr><td><strong>Original 24h Volume</strong></td><td>{original_24h_gb:.4f} GB</td></tr>
                      <tr><td><strong>Est. Dropped per Day (from last {hours}h)</strong></td><td style="color: green; font-weight: bold;">{dropped_hours_gb:.4f} GB</td></tr>
                      <tr><td><strong>Effective Post-Drop Volume</strong></td><td>{final_24h_gb:.4f} GB</td></tr>
                    </table>
                  </body>
                </html>
                """
                send_html_email(subject, html_body, alert_emails, config_file=config_file)
                return f"<h1>Drop Rule Applied for {app_name}!</h1><p>Confirmation email sent.</p>"

        except Exception as e:
            return f"<h1>Error executing log drop:</h1><p>{escape(str(e))}</p>", 500


@app.route('/api/start-workflow', methods=['GET'])
def api_start_workflow():
    account_id = request.args.get('account_id', '8124546')
    config_file = request.args.get('config', 'config.json')

    try:
        run_id = trigger_workflow_run(
            workflow_name="logsIngestionAnalysis1",
            account_id=account_id,
            agent_id="ask_ai_v2",
            config_file=config_file,
            # Only if the workflow declares an 'ingestionContext' input (see workflows/logsIngestionAnalysis1.yaml)
            extra_inputs=({"ingestionContext": build_ingestion_context(account_id, config_file=config_file)}
                          if load_config(config_file).get("workflow", {}).get("sendIngestionContext") else None)
        )
        return jsonify({"run_id": run_id, "status": "STARTED"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/workflow-status/<run_id>', methods=['GET'])
def api_workflow_status(run_id):
    account_id = request.args.get('account_id', '8124546')
    config_file = request.args.get('config', 'config.json')

    result = poll_workflow_status(run_id=run_id, account_id=account_id, config_file=config_file)
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)