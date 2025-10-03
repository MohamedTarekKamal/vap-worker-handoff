#!/usr/bin/env python3
"""
poc_runner_sqlmap.py
See usage in comments.
"""
import argparse, json, os, time, shlex, subprocess
from urllib.parse import urljoin
import requests

parser = argparse.ArgumentParser()
parser.add_argument('--payload', required=True, help='path to payload.json')
parser.add_argument('--outdir', required=True, help='save directory')
args = parser.parse_args()

with open(args.payload, 'r', encoding='utf-8') as f:
    payload = json.load(f)

target = payload['target']['url']
poc = payload.get('finding', {}).get('poc', {})
scan_opts = payload.get('scan_profile', {}).get('options', {})
use_sqlmap = bool(scan_opts.get('use_sqlmap', False))

os.makedirs(args.outdir, exist_ok=True)

# --- perform initial safe request to capture response snippet ---
method = poc.get('method', 'GET').upper()
path = poc.get('path', '') or ''
body = poc.get('body')
headers = poc.get('headers') or {}
timeout = scan_opts.get('timeout', 30)

def build_full_url(base, path):
    if path.startswith('http://') or path.startswith('https://'):
        return path
    return base.rstrip('/') + '/' + path.lstrip('/')

full_url = build_full_url(target, path)

summary = {
    "target": target,
    "poc_url": full_url,
    "method": method,
    "ran_at": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    "http_probe": None,
    "sqlmap": None
}

# safe probe
try:
    if method == 'GET':
        r = requests.get(full_url, headers=headers, timeout=timeout, allow_redirects=True)
    elif method == 'HEAD':
        r = requests.head(full_url, headers=headers, timeout=timeout, allow_redirects=True)
    elif method == 'POST':
        if isinstance(body, dict):
            r = requests.post(full_url, headers=headers, data=body, timeout=timeout, allow_redirects=True)
        else:
            r = requests.post(full_url, headers=headers, data=body, timeout=timeout, allow_redirects=True)
    else:
        r = None
    if r is not None:
        summary['http_probe'] = {
            "status_code": r.status_code,
            "final_url": r.url,
            "headers": dict(r.headers),
            "response_snippet": (r.text or "")[:2000]
        }
except Exception as e:
    summary['http_probe'] = {"error": str(e)}

# --- optional: run sqlmap (controlled) ---
if use_sqlmap:
    try:
        from shutil import which
        sqlmap_path = which('sqlmap')
    except Exception:
        sqlmap_path = None

    if not sqlmap_path:
        summary['sqlmap'] = {"error": "sqlmap not found on PATH. Install sqlmap or set path."}
    else:
        level = int(scan_opts.get('sqlmap_level', 1))
        risk = int(scan_opts.get('sqlmap_risk', 1))
        timeout_opt = int(scan_opts.get('timeout', 120))
        sqlmap_out = os.path.join(args.outdir, 'sqlmap_output.txt')
        sqlmap_cmd = [sqlmap_path, '-u', full_url, '--batch', '--level', str(level), '--risk', str(risk),
                      '--output-dir', args.outdir]
        if method == 'POST' and body:
            if isinstance(body, dict):
                from urllib.parse import urlencode
                data_str = urlencode(body)
            else:
                data_str = str(body)
            sqlmap_cmd.extend(['--data', data_str])
        try:
            proc = subprocess.run(sqlmap_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_opt)
            sql_stdout = proc.stdout.decode(errors='ignore')
            sql_stderr = proc.stderr.decode(errors='ignore')
            with open(sqlmap_out, 'w', encoding='utf-8') as f:
                f.write("=== SQLMAP STDOUT ===\n")
                f.write(sql_stdout)
                f.write("\n\n=== SQLMAP STDERR ===\n")
                f.write(sql_stderr)
            summary['sqlmap'] = {
                "exit_code": proc.returncode,
                "output_file": sqlmap_out,
                "stdout_snippet": sql_stdout[:2000],
                "stderr_snippet": sql_stderr[:2000]
            }
        except subprocess.TimeoutExpired as e:
            summary['sqlmap'] = {"error": "sqlmap timed out", "details": str(e)}
        except Exception as e:
            summary['sqlmap'] = {"error": str(e)}

summary_path = os.path.join(args.outdir, 'summary.json')
with open(summary_path, 'w', encoding='utf-8') as f:
    json.dump(summary, f, indent=2, ensure_ascii=False)

print(json.dumps({"ok": True, "summary_path": summary_path}))
