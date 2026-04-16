# [FILE 1: app.py (Unified)]
# Paste this into your GitHub 'app.py' editor.
# This contains the full application logic.

import os
import json
import time
import webbrowser
import re
import sqlite3
import requests
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from flask import Flask, render_template, request, session, redirect, url_for, Response, jsonify, send_file
from requests.auth import HTTPBasicAuth

# Optional: psycopg2 for Postgres support on cloud
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

class DataForSeoClient:
    BASE_URL = "https://api.dataforseo.com/v3"

    def __init__(self, login, password):
        self.login = login
        self.password = password
        self.auth = HTTPBasicAuth(login, password)
        self.session = requests.Session()

    def _post(self, endpoint, data, retries=3):
        url = f"{self.BASE_URL}{endpoint}"
        for i in range(retries):
            try:
                response = self.session.post(url, auth=self.auth, json=data, timeout=120)
                if response.status_code == 429:
                    wait_time = (2 ** i) * 2
                    time.sleep(wait_time)
                    continue
                if not response.ok:
                    try:
                        err_data = response.json()
                        msg = err_data.get('status_message', response.reason)
                        raise requests.exceptions.RequestException(f"{response.status_code} {msg}")
                    except ValueError:
                        response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                if i == retries - 1:
                    raise e
                time.sleep(2)
        return None

    def get_google_ai_mode(self, keyword, location_name, language_code):
        endpoint = "/serp/google/ai_mode/live/advanced"
        payload = [{"keyword": keyword, "location_name": location_name, "language_code": language_code}]
        return self._post(endpoint, payload)

    def get_llm_response(self, platform, model_name, prompt):
        endpoint = f"/ai_optimization/{platform}/llm_responses/live"
        payload = [{"user_prompt": prompt, "model_name": model_name, "web_search": True, "max_output_tokens": 1000}]
        return self._post(endpoint, payload)

    def get_llm_mentions(self, brand_name, platform="google"):
        endpoint = "/ai_optimization/llm_mentions/search/live"
        payload = [{"target": {"keyword": brand_name}, "platform": platform}]
        return self._post(endpoint, payload)

    def parse_google_ai_mode(self, response, brand_domain, brand_name, competitors):
        try:
            tasks = response.get('tasks', [])
            if not tasks: return {"mentioned": None, "position": None, "sources": [], "ai_text": "No tasks in response", "competitor_mentions": {}}
            result_list = tasks[0].get('result', [])
            if not result_list: return {"mentioned": False, "position": None, "sources": [], "ai_text": "No AI Overview found", "competitor_mentions": {}}
            items = result_list[0].get('items', [])
            if not items: return {"mentioned": False, "position": None, "sources": [], "ai_text": "No items in AI Overview", "competitor_mentions": {}}
            item = items[0]
            ai_text = item.get('markdown', '')
            refs = item.get('references', []) or []
            sources = [ref.get('url') for ref in refs if ref and ref.get('url')]
            return self._detect_mentions(ai_text, sources, brand_domain, brand_name, competitors)
        except Exception as e:
            return {"mentioned": None, "position": None, "sources": [], "ai_text": f"Parse Error: {repr(e)}", "competitor_mentions": {}}

    def parse_llm_response(self, response, brand_domain, brand_name, competitors):
        try:
            tasks = response.get('tasks', [])
            if not tasks: return {"mentioned": None, "position": None, "sources": [], "ai_text": "No tasks in response", "competitor_mentions": {}}
            result_list = tasks[0].get('result', [])
            if not result_list: return {"mentioned": False, "position": None, "sources": [], "ai_text": "No result from LLM", "competitor_mentions": {}}
            items = result_list[0].get('items', [])
            if not items: return {"mentioned": False, "position": None, "sources": [], "ai_text": "Empty LLM items", "competitor_mentions": {}}
            item = items[0]
            sections = item.get('sections', [])
            ai_text = "\n".join([s.get('text', '') for s in sections if s.get('text')])
            sources = []
            for s in sections:
                for ann in s.get('annotations', []) or []:
                    if ann.get('url'): sources.append(ann.get('url'))
            return self._detect_mentions(ai_text, sources, brand_domain, brand_name, competitors)
        except Exception as e:
            return {"mentioned": None, "position": None, "sources": [], "ai_text": f"Parse Error: {repr(e)}", "competitor_mentions": {}}

    def _detect_mentions(self, text, sources, brand_domain, brand_name, competitors):
        text_clean = re.sub(r'[^\x20-\x7E\s]', '', text).strip()
        text_lower = text_clean.lower()
        brand_domain_l = brand_domain.lower() if brand_domain else ""
        brand_name_l = brand_name.lower() if brand_name else ""
        patterns = []
        if brand_name_l:
            patterns.append(re.escape(brand_name_l))
            if len(brand_name_l) > 4:
                half = len(brand_name_l) // 2
                patterns.append(re.escape(brand_name_l[:half]) + r'\s*' + re.escape(brand_name_l[half:]))
            patterns.append(re.escape(brand_name_l) + r'\s*(pvt|ltd|pvt\s+ltd|inc|corp|group|informatics|solutions)')
        mentioned_in_text = False
        for pattern in patterns:
            if re.search(pattern, text_lower):
                mentioned_in_text = True
                break
        if not mentioned_in_text and brand_domain_l and brand_domain_l in text_lower:
            mentioned_in_text = True
        mentioned_in_sources = False
        for src in (sources or []):
            src_l = src.lower()
            if brand_domain_l and brand_domain_l in src_l:
                mentioned_in_sources = True
                break
            for pattern in patterns:
                if re.search(pattern, src_l):
                    mentioned_in_sources = True
                    break
            if mentioned_in_sources: break
        mentioned = mentioned_in_text or mentioned_in_sources
        position = None
        if mentioned_in_text:
            earliest = len(text_lower)
            for pattern in patterns:
                m = re.search(pattern, text_lower)
                if m and m.start() < earliest: earliest = m.start()
            if earliest < len(text_lower): position = text_clean[:earliest].count('\n') + 1
        competitor_mentions = {}
        for comp in competitors:
            comp_l = comp.lower()
            count = text_lower.count(comp_l)
            for src in (sources or []):
                if comp_l in src.lower(): count += 1
            if count > 0: competitor_mentions[comp] = count
        return {"mentioned": mentioned, "position": position, "sources": sources, "ai_text": text_clean, "competitor_mentions": competitor_mentions}

class TrackerStorage:
    def __init__(self, db_path=None):
        self.db_url = os.environ.get('POSTGRES_URL')
        if not self.db_url:
            self.db_path = Path(db_path or "data/tracker.db")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.mode = 'sqlite'
        else:
            self.mode = 'postgres'
        self._init_db()

    def _get_connection(self):
        if self.mode == 'postgres':
            url = self.db_url.replace("postgres://", "postgresql://")
            return psycopg2.connect(url)
        else:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

    def _init_db(self):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            serial_pk = "SERIAL PRIMARY KEY" if self.mode == 'postgres' else "INTEGER PRIMARY KEY AUTOINCREMENT"
            cur.execute(f"CREATE TABLE IF NOT EXISTS runs (id {serial_pk}, brand_domain TEXT, brand_name TEXT, country TEXT, language TEXT, run_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            cur.execute(f"CREATE TABLE IF NOT EXISTS mention_results (id {serial_pk}, run_id INTEGER, keyword TEXT, platform TEXT, mentioned BOOLEAN, mention_position INTEGER, sources_cited TEXT, competitor_mentions TEXT, ai_response_text TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            cur.execute(f"CREATE TABLE IF NOT EXISTS competitor_metrics (id {serial_pk}, run_id INTEGER, domain TEXT, total_mentions INTEGER DEFAULT 0, avg_position REAL, share_of_voice REAL)")
            cur.execute(f"CREATE TABLE IF NOT EXISTS discovery_results (id {serial_pk}, brand_name TEXT, keyword TEXT, platform TEXT, mentions_count INTEGER DEFAULT 0, quoted_links TEXT, cross_platform_mentions TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            conn.commit()
        finally: conn.close()

    def create_run(self, brand_domain, brand_name, country, language):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.mode == 'postgres':
                cur.execute("INSERT INTO runs (brand_domain, brand_name, country, language) VALUES (%s, %s, %s, %s) RETURNING id", (brand_domain, brand_name, country, language))
                run_id = cur.fetchone()[0]
            else:
                cur.execute("INSERT INTO runs (brand_domain, brand_name, country, language) VALUES (?, ?, ?, ?)", (brand_domain, brand_name, country, language))
                run_id = cur.lastrowid
            conn.commit()
            return run_id
        finally: conn.close()

    def save_mention_result(self, run_id, keyword, platform, result):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            sql = "INSERT INTO mention_results (run_id, keyword, platform, mentioned, mention_position, sources_cited, competitor_mentions, ai_response_text) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)" if self.mode == 'postgres' else "INSERT INTO mention_results (run_id, keyword, platform, mentioned, mention_position, sources_cited, competitor_mentions, ai_response_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            cur.execute(sql, (run_id, keyword, platform, result.get('mentioned'), result.get('position'), json.dumps(result.get('sources', [])), json.dumps(result.get('competitor_mentions', {})), result.get('ai_text')))
            conn.commit()
        finally: conn.close()

    def get_run(self, run_id):
        conn = self._get_connection()
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor) if self.mode == 'postgres' else conn.cursor()
            sql = "SELECT * FROM runs WHERE id = %s" if self.mode == 'postgres' else "SELECT * FROM runs WHERE id = ?"
            cur.execute(sql, (run_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally: conn.close()

    def get_results(self, run_id):
        conn = self._get_connection()
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor) if self.mode == 'postgres' else conn.cursor()
            cur.execute("SELECT * FROM mention_results WHERE run_id = %s" if self.mode == 'postgres' else "SELECT * FROM mention_results WHERE run_id = ?", (run_id,))
            rows = cur.fetchall()
            results = []
            for row in rows:
                res = dict(row)
                res['sources_cited'] = json.loads(row['sources_cited']) if row['sources_cited'] else []
                res['competitor_mentions'] = json.loads(row['competitor_mentions']) if row['competitor_mentions'] else {}
                results.append(res)
            return results
        finally: conn.close()

    def save_competitor_metrics(self, run_id, metrics):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            sql = "INSERT INTO competitor_metrics (run_id, domain, total_mentions, avg_position, share_of_voice) VALUES (%s, %s, %s, %s, %s)" if self.mode == 'postgres' else "INSERT INTO competitor_metrics (run_id, domain, total_mentions, avg_position, share_of_voice) VALUES (?, ?, ?, ?, ?)"
            for m in metrics: cur.execute(sql, (run_id, m['domain'], m['total_mentions'], m['avg_position'], m['share_of_voice']))
            conn.commit()
        finally: conn.close()

    def get_competitor_metrics(self, run_id):
        conn = self._get_connection()
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor) if self.mode == 'postgres' else conn.cursor()
            cur.execute("SELECT * FROM competitor_metrics WHERE run_id = %s" if self.mode == 'postgres' else "SELECT * FROM competitor_metrics WHERE run_id = ?", (run_id,))
            return [dict(r) for r in cur.fetchall()]
        finally: conn.close()

    def get_history(self, brand_domain):
        conn = self._get_connection()
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor) if self.mode == 'postgres' else conn.cursor()
            sql = "SELECT r.id, r.run_date, SUM(CASE WHEN m.mentioned THEN 1 ELSE 0 END) as total_mentions FROM runs r LEFT JOIN mention_results m ON r.id = m.run_id WHERE r.brand_domain = %s GROUP BY r.id, r.run_date ORDER BY r.run_date ASC" if self.mode == 'postgres' else "SELECT r.id, r.run_date, SUM(m.mentioned) as total_mentions FROM runs r LEFT JOIN mention_results m ON r.id = m.run_id WHERE r.brand_domain = ? GROUP BY r.id ORDER BY r.run_date ASC"
            cur.execute(sql, (brand_domain,))
            return [dict(r) for r in cur.fetchall()]
        finally: conn.close()

    def save_discovery_results(self, brand_name, platform, results):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            sql = "INSERT INTO discovery_results (brand_name, platform, keyword, mentions_count, quoted_links, cross_platform_mentions) VALUES (%s, %s, %s, %s, %s, %s)" if self.mode == 'postgres' else "INSERT INTO discovery_results (brand_name, platform, keyword, mentions_count, quoted_links, cross_platform_mentions) VALUES (?, ?, ?, ?, ?, ?)"
            for item in results: cur.execute(sql, (brand_name, platform, item.get('keyword'), item.get('mentions_count'), json.dumps(item.get('quoted_links', [])), json.dumps(item.get('cross_platform_mentions', {}))))
            conn.commit()
        finally: conn.close()

    def get_discovery_results(self, brand_name):
        conn = self._get_connection()
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor) if self.mode == 'postgres' else conn.cursor()
            cur.execute("SELECT * FROM discovery_results WHERE brand_name = %s ORDER BY timestamp DESC" if self.mode == 'postgres' else "SELECT * FROM discovery_results WHERE brand_name = ? ORDER BY timestamp DESC", (brand_name,))
            rows = cur.fetchall()
            results = []
            for row in rows:
                res = dict(row)
                res['quoted_links'] = json.loads(row['quoted_links']) if row['quoted_links'] else []
                res['cross_platform_mentions'] = json.loads(row['cross_platform_mentions']) if row['cross_platform_mentions'] else {}
                results.append(res)
            return results
        finally: conn.close()

app = Flask(__name__)
app.secret_key = os.urandom(24)
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "tracker.db"
storage = TrackerStorage(DB_PATH)

PLATFORMS = [
    {"id": "google", "name": "Google AI Mode"},
    {"id": "chat_gpt", "name": "ChatGPT", "model": "gpt-4.1-mini"},
    {"id": "perplexity", "name": "Perplexity", "model": "sonar"},
    {"id": "gemini", "name": "Gemini", "model": "gemini-2.0-flash"},
    {"id": "claude", "name": "Claude", "model": "claude-haiku-4-5"}
]

@app.route("/")
def index():
    return render_template("setup.html", active_tab=request.args.get('tab', 'monitor'))

@app.route("/api/run", methods=["POST"])
def run_tracker():
    data = request.get_json()
    session['credentials'] = {'login': data.get('api_login'), 'password': data.get('api_password')}
    high_vol = [k.strip() for k in data.get('high_volume_keywords', '').split('\n') if k.strip()]
    brand_niche = [k.strip() for k in data.get('brand_niche_keywords', '').split('\n') if k.strip()]
    keywords = list(set(high_vol + brand_niche))
    session['tracker_config'] = {
        'brand_domain': data.get('brand_domain'),
        'brand_name': data.get('brand_name'),
        'country': data.get('country', 'India'),
        'location': data.get('location', data.get('country', 'India')),
        'language': data.get('language', 'en'),
        'competitors': [c.strip() for c in data.get('competitors', []) if c.strip()],
        'keywords': keywords
    }
    return jsonify({"status": "success", "redirect": url_for('running')})

@app.route("/api/discover", methods=["POST"])
def discover_citations():
    data = request.get_json()
    brand_name = data.get('brand_name')
    creds = {'login': data.get('api_login'), 'password': data.get('api_password')}
    client = DataForSeoClient(creds['login'], creds['password'])
    keyword_map = {}
    kws_list = []
    for platform in ["google", "chat_gpt"]:
        resp = client.get_llm_mentions(brand_name, platform)
        if resp and 'tasks' in resp:
            items = resp['tasks'][0].get('result', [{}])[0].get('items', [])
            for item in items:
                kw = item.get('keyword')
                if kw and kw not in keyword_map:
                    kws_list.append(kw)
                    keyword_map[kw] = {"platform": platform, "mentions_count": item.get('mentions_count', 0), "quoted_links": item.get('quoted_links', [])}
    all_results = []
    def verify_kw(kw):
        cross = {}
        for p in PLATFORMS:
            res = client.parse_google_ai_mode(client.get_google_ai_mode(kw, "India", "en"), "", "", []) if p['id'] == 'google' else client.parse_llm_response(client.get_llm_response(p['id'], p['model'], kw), "", "", [])
            cross[p['id']] = res.get('mentioned', False)
        return (kw, cross)
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(verify_kw, kw): kw for kw in kws_list[:10]}
        for f in as_completed(futures):
            kw, cross = f.result()
            base = keyword_map[kw]
            all_results.append({"keyword": kw, "platform": base["platform"], "mentions_count": base["mentions_count"], "quoted_links": base["quoted_links"], "cross_platform_mentions": cross})
    storage.save_discovery_results(brand_name, "Deep Aggregated", all_results)
    return jsonify({"status": "success", "count": len(all_results)})

@app.route("/discovery/<brand_name>")
def discovery_results_view(brand_name):
    return render_template("discovery_results.html", brand_name=brand_name, results=storage.get_discovery_results(brand_name))

@app.route("/running")
def running():
    return render_template("running.html")

@app.route("/stream")
def stream():
    config, creds = session.get('tracker_config'), session.get('credentials')
    run_id = storage.create_run(config['brand_domain'], config['brand_name'], config['country'], config['language'])
    def generate():
        client = DataForSeoClient(creds['login'], creds['password'])
        total, completed = len(config['keywords']) * len(PLATFORMS), 0
        db_lock = Lock()
        tasks = [(kw, p) for kw in config['keywords'] for p in PLATFORMS]
        def process_task(t):
            kw, p = t
            res = client.parse_google_ai_mode(client.get_google_ai_mode(kw, config['location'], config['language']), config['brand_domain'], config['brand_name'], config['competitors']) if p['id'] == 'google' else client.parse_llm_response(client.get_llm_response(p['id'], p['model'], kw), config['brand_domain'], config['brand_name'], config['competitors'])
            with db_lock: storage.save_mention_result(run_id, kw, p['id'], res)
            return (kw, p['name'], res)
        with ThreadPoolExecutor(max_workers=20) as ex:
            for f in as_completed({ex.submit(process_task, t): t for t in tasks}):
                completed += 1
                kw, p_name, res = f.result()
                yield f"data: {json.dumps({'progress': int((completed/total)*100), 'message': f'[{completed}/{total}] {kw} in {p_name}: {res.get(\"mentioned\")}', 'status': 'running'})}\n\n"
        results = storage.get_results(run_id)
        comp_metrics = []
        for domain in [config['brand_domain']] + config['competitors']:
            mentions, pos = 0, []
            for r in results:
                if domain == config['brand_domain']:
                    if r['mentioned']:
                        mentions += 1
                        if r['mention_position']: pos.append(r['mention_position'])
                elif r['competitor_mentions'].get(domain, 0) > 0: mentions += 1
            comp_metrics.append({"domain": domain, "total_mentions": mentions, "avg_position": sum(pos)/len(pos) if pos else 0, "share_of_voice": (mentions/total)*100})
        storage.save_competitor_metrics(run_id, comp_metrics)
        yield f"data: {json.dumps({'progress': 100, 'message': 'Completed!', 'status': 'done', 'run_id': run_id})}\n\n"
    return Response(generate(), headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route("/dashboard/<int:run_id>")
def dashboard(run_id):
    run = storage.get_run(run_id)
    results = storage.get_results(run_id)
    platform_data = {p['name']: sum(1 for r in results if r['platform'] == p['id'] and r['mentioned']) for p in PLATFORMS}
    comp_mentions = {}
    for r in results:
        for c, count in r['competitor_mentions'].items(): comp_mentions[c] = comp_mentions.get(c, 0) + count
    return render_template("dashboard.html", run=run, results=results, platforms=PLATFORMS, comp_metrics=storage.get_competitor_metrics(run_id), history=storage.get_history(run['brand_domain']), platform_data=platform_data, top_competitors=sorted(comp_mentions.items(), key=lambda x: x[1], reverse=True)[:5])

if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
