import os
import json
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from flask import Flask, render_template, request, session, redirect, url_for, Response, jsonify, send_file
from api.dataforseo import DataForSeoClient
from db.storage import TrackerStorage

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Paths
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "tracker.db"

# Initialize Storage
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
    active_tab = request.args.get('tab', 'monitor')
    return render_template("setup.html", active_tab=active_tab)

@app.route("/api/run", methods=["POST"])
def run_tracker():
    data = request.get_json()
    
    # Store config in session
    session['credentials'] = {
        'login': data.get('api_login'),
        'password': data.get('api_password')
    }
    
    # Extract keywords (one per line)
    high_vol = [k.strip() for k in data.get('high_volume_keywords', '').split('\n') if k.strip()]
    brand_niche = [k.strip() for k in data.get('brand_niche_keywords', '').split('\n') if k.strip()]
    keywords = list(set(high_vol + brand_niche))
    
    competitors = [c.strip() for c in data.get('competitors', []) if c.strip()]
    
    config = {
        'brand_domain': data.get('brand_domain'),
        'brand_name': data.get('brand_name'),
        'country': data.get('country', 'India'),
        'language': data.get('language', 'en'),
        'competitors': competitors,
        'keywords': keywords
    }
    session['tracker_config'] = config
    
    return jsonify({"status": "success", "redirect": url_for('running')})

@app.route("/api/discover", methods=["POST"])
def discover_citations():
    data = request.get_json()
    brand_name = data.get('brand_name')
    creds = {
        'login': data.get('api_login'),
        'password': data.get('api_password')
    }
    
    if not brand_name or not creds['login']:
        return jsonify({"error": "Missing brand name or credentials"}), 400

    client = DataForSeoClient(creds['login'], creds['password'])
    discovery_platforms = ["google", "chat_gpt"] # Supported by mentions search API
    
    initial_keywords = []
    keyword_map = {} # keyword -> {original_platform, mentions_count, quoted_links}
    
    try:
        # Step 1: Surface Scan (Google/ChatGPT Mentions API)
        for platform in discovery_platforms:
            response = client.get_llm_mentions(brand_name, platform)
            if response and 'tasks' in response:
                result_list = response['tasks'][0].get('result', [])
                if result_list:
                    items = result_list[0].get('items', [])
                    for item in items:
                        kw = item.get('keyword')
                        if kw and kw not in keyword_map:
                            initial_keywords.append(kw)
                            keyword_map[kw] = {
                                "platform": platform,
                                "mentions_count": item.get('mentions_count', 0),
                                "quoted_links": item.get('quoted_links', [])
                            }
        
        if not initial_keywords:
            return jsonify({"status": "empty", "message": "No citations found for this brand."})

        # Step 2: Deep Verification (Cross-check discovered keywords on ALL platforms)
        # Assuming brand_domain might be unknown, we use name-based regex.
        # But for accuracy, prompt-based lookup is best.
        all_results = []
        
        def verify_kw(kw):
            cross_mentions = {}
            for p in PLATFORMS:
                try:
                    p_id = p['id']
                    if p_id == 'google':
                        # Default to India/en for cross-check discovery
                        resp = client.get_google_ai_mode(kw, "India", "en")
                        res = client.parse_google_ai_mode(resp, "", "", [])
                    else:
                        resp = client.get_llm_response(p_id, p['model'], kw)
                        res = client.parse_llm_response(resp, "", "", [])
                    cross_mentions[p_id] = res.get('mentioned', False)
                except Exception as e:
                    print(f"Deep check error ({p_id}): {e}")
                    cross_mentions[p['id']] = False
            return (kw, cross_mentions)

        # limit to top 10 discovered keywords to avoid huge latencies
        test_kws = initial_keywords[:10]
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_kw = {executor.submit(verify_kw, kw): kw for kw in test_kws}
            for future in as_completed(future_to_kw):
                kw, cross_mentions = future.result()
                base = keyword_map[kw]
                all_results.append({
                    "keyword": kw,
                    "platform": base["platform"],
                    "mentions_count": base["mentions_count"],
                    "quoted_links": base["quoted_links"],
                    "cross_platform_mentions": cross_mentions
                })

        if all_results:
            storage.save_discovery_results(brand_name, "Deep Aggregated", all_results)
            return jsonify({"status": "success", "count": len(all_results)})
        else:
            return jsonify({"status": "empty", "message": "No verifiable citations found."})
            
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

@app.route("/discovery/<brand_name>")
def discovery_results(brand_name):
    results = storage.get_discovery_results(brand_name)
    return render_template("discovery_results.html", brand_name=brand_name, results=results)

@app.route("/running")
def running():
    if 'tracker_config' not in session:
        return redirect(url_for('index'))
    return render_template("running.html")

@app.route("/stream")
def stream():
    # SSE CRITICAL: Extract session data BEFORE the generator
    config = session.get('tracker_config')
    creds = session.get('credentials')
    
    if not config or not creds:
        return Response("data: {\"error\": \"Session expired\"}\n\n", mimetype='text/event-stream')

    # Create run in DB
    run_id = storage.create_run(
        config['brand_domain'], 
        config['brand_name'], 
        config['country'], 
        config['language']
    )
    session['last_run_id'] = run_id

    def generate(config, creds, run_id):
        client = DataForSeoClient(creds['login'], creds['password'])
        keywords = config['keywords']
        brand_domain = config['brand_domain']
        brand_name = config['brand_name']
        competitors = config['competitors']
        total_steps = len(keywords) * len(PLATFORMS)
        completed_steps = 0
        db_lock = Lock()

        tasks = []
        for keyword in keywords:
            for platform in PLATFORMS:
                tasks.append((keyword, platform))

        def process_task(task):
            keyword, platform = task
            platform_id = platform['id']
            try:
                if platform_id == 'google':
                    response = client.get_google_ai_mode(keyword, config['country'], config['language'])
                    result = client.parse_google_ai_mode(response, brand_domain, brand_name, competitors)
                else:
                    response = client.get_llm_response(platform_id, platform['model'], keyword)
                    result = client.parse_llm_response(response, brand_domain, brand_name, competitors)
                
                with db_lock:
                    storage.save_mention_result(run_id, keyword, platform_id, result)
                return (keyword, platform['name'], result, None)
            except Exception as e:
                with db_lock:
                    storage.save_mention_result(run_id, keyword, platform_id, {"mentioned": None, "ai_text": str(e)})
                return (keyword, platform['name'], None, str(e))

        # Use ThreadPoolExecutor for parallel API calls (max 10 concurrent tasks)
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_task = {executor.submit(process_task, t): t for t in tasks}
            
            for future in as_completed(future_to_task):
                completed_steps += 1
                keyword, platform_name, result, error = future.result()
                
                progress = int((completed_steps / total_steps) * 100)
                msg = f"[{completed_steps}/{total_steps}] \"{keyword}\" → {platform_name}..."
                
                if error:
                    msg += f" ✗ error: {error}"
                else:
                    msg += f" ✓ {'mentioned' if result['mentioned'] else 'not mentioned'}"
                    if result['mentioned'] and result['position']:
                        msg += f" (pos: {result['position']})"

                payload = {
                    "progress": progress,
                    "message": msg,
                    "status": "running"
                }
                yield f"data: {json.dumps(payload)}\n\n"

        # After all checks, compute competitor share of voice
        results = storage.get_results(run_id)
        domains = [brand_domain] + competitors
        total_checks = len(keywords) * len(PLATFORMS)
        
        comp_metrics = []
        for domain in domains:
            mentions = 0
            positions = []
            for res in results:
                if domain == brand_domain:
                    if res['mentioned']:
                        mentions += 1
                        if res['mention_position']: positions.append(res['mention_position'])
                else:
                    count = res['competitor_mentions'].get(domain, 0)
                    if count > 0:
                        mentions += 1
            
            sov = (mentions / total_checks) * 100 if total_checks > 0 else 0
            avg_pos = sum(positions) / len(positions) if positions else 0
            
            comp_metrics.append({
                "domain": domain,
                "total_mentions": mentions,
                "avg_position": avg_pos,
                "share_of_voice": sov
            })
        
        storage.save_competitor_metrics(run_id, comp_metrics)

        # Final message
        yield f"data: {json.dumps({'progress': 100, 'message': 'Completed!', 'status': 'done', 'run_id': run_id})}\n\n"

    return Response(generate(config, creds, run_id), mimetype='text/event-stream')

@app.route("/dashboard/<int:run_id>")
def dashboard(run_id):
    run = storage.get_run(run_id)
    if not run:
        return "Run not found", 404
    
    results = storage.get_results(run_id)
    comp_metrics = storage.get_competitor_metrics(run_id)
    history = storage.get_history(run['brand_domain'])
    
    # Platform breakdown
    platform_data = {}
    for p in PLATFORMS:
        mentions = sum(1 for r in results if r['platform'] == p['id'] and r['mentioned'])
        platform_data[p['name']] = mentions

    all_competitor_mentions = {}
    for r in results:
        for comp, count in r['competitor_mentions'].items():
            all_competitor_mentions[comp] = all_competitor_mentions.get(comp, 0) + count
    top_competitors = sorted(all_competitor_mentions.items(), key=lambda x: x[1], reverse=True)[:5]

    # Convert Row objects to dicts for JSON serialization in template
    run_dict = dict(run)
    comp_metrics_list = [dict(row) for row in comp_metrics]
    history_list = [dict(row) for row in history]
    
    return render_template(
        "dashboard.html",
        run=run_dict,
        results=results,
        platforms=PLATFORMS,
        comp_metrics=comp_metrics_list,
        history=history_list,
        platform_data=platform_data,
        top_competitors=top_competitors
    )

@app.route("/download/<int:run_id>")
def download_report(run_id):
    run = storage.get_run(run_id)
    if not run:
        return "Run not found", 404
    
    results = storage.get_results(run_id)
    comp_metrics = storage.get_competitor_metrics(run_id)
    history = storage.get_history(run['brand_domain'])
    
    platform_data = {}
    for p in PLATFORMS:
        mentions = sum(1 for r in results if r['platform'] == p['id'] and r['mentioned'])
        platform_data[p['name']] = mentions
        
    all_competitor_mentions = {}
    for r in results:
        for comp, count in r['competitor_mentions'].items():
            all_competitor_mentions[comp] = all_competitor_mentions.get(comp, 0) + count
    top_competitors = sorted(all_competitor_mentions.items(), key=lambda x: x[1], reverse=True)[:5]

    # Convert for report
    run_dict = dict(run)
    comp_metrics_list = [dict(row) for row in comp_metrics]
    history_list = [dict(row) for row in history]

    # Render as static
    html_content = render_template(
        "dashboard.html",
        run=run_dict,
        results=results,
        platforms=PLATFORMS,
        comp_metrics=comp_metrics_list,
        history=history_list,
        platform_data=platform_data,
        top_competitors=top_competitors,
        is_report=True
    )
    
    report_filename = f"AI-Mention-Report-{run['brand_domain']}-{datetime.now().strftime('%Y-%m-%d')}.html"
    report_path = DATA_DIR / report_filename
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    
    return send_file(report_path, as_attachment=True)

if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    port = int(os.environ.get("PORT", 5000))
    
    print(f"\n+ AI Mention Tracker is starting on port {port}...")
    
    # Only open browser locally
    if os.environ.get("VERCEL") is None and os.environ.get("RENDER") is None:
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except:
            pass
            
    app.run(host="0.0.0.0", port=port)
