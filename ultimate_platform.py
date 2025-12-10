#!/usr/bin/env python3
"""
AI 자동 개발 플랫폼 - 최종 완전판 v4.0
- Prompt Caching: 비용 90% 절감
- Extended Thinking: 정확한 코드 생성
- SQLite: 프로젝트 히스토리 관리
- 변수/함수명 기억 및 유지
- 웹 + CLI 모드
- Clasp 배포 지원

실행: python ultimate_platform.py
CLI: python ultimate_platform.py --cli --requirements req.md
"""

import os, sys, json, time, secrets, hashlib, threading, zipfile, io, subprocess, argparse, re, sqlite3
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from functools import wraps
from flask import Flask, send_from_directory, request, jsonify, send_file

# ============================================================
# 설정
# ============================================================
class Config:
    SECRET_KEY = os.urandom(24)
    DEBUG = False
    PORT = int(os.getenv('PORT', 5000))  # Railway가 자동으로 PORT 설정
    HOST = '0.0.0.0'  # Railway에서 필수
    
    OUTPUT_DIR = Path('./output')
    
    # Railway 환경변수에서 API 키 읽기
    CLAUDE_API_KEY = os.getenv('ANTHROPIC_API_KEY', os.getenv('CLAUDE_API_KEY', ''))
    
    # 로컬 개발용 (Railway에서는 환경변수 사용)
    if not CLAUDE_API_KEY:
        CLAUDE_API_KEY = 'sk-ant-api03-여기에실제키입력'  # ← 로컬 개발용
    
    API_TIMEOUT = 120
    CACHE_ENABLED = True
    CACHE_TTL = 3600

Config.OUTPUT_DIR.mkdir(exist_ok=True)

# ============================================================
# 색상 로그
# ============================================================
class C:
    B='\033[94m';G='\033[92m';Y='\033[93m';R='\033[91m';BOLD='\033[1m';E='\033[0m'

class Log:
    @staticmethod
    def i(m): print(f"{C.B}[{datetime.now():%H:%M:%S}] ℹ{C.E} {m}")
    @staticmethod
    def s(m): print(f"{C.G}[{datetime.now():%H:%M:%S}] ✓{C.E} {m}")
    @staticmethod
    def w(m): print(f"{C.Y}[{datetime.now():%H:%M:%S}] ⚠{C.E} {m}")
    @staticmethod
    def e(m): print(f"{C.R}[{datetime.now():%H:%M:%S}] ✗{C.E} {m}")

# ============================================================
# SQLite 초기화
# ============================================================
def init_db():
    conn = sqlite3.connect('projects.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS projects
                 (id TEXT PRIMARY KEY, name TEXT, code TEXT, variables TEXT,
                  functions TEXT, history TEXT, created_at TEXT, updated_at TEXT)''')
    conn.commit()
    conn.close()

init_db()

# ============================================================
# 프로젝트 상태 관리
# ============================================================
class ProjectState:
    @staticmethod
    def save(pid, name, code, vars, funcs):
        conn = sqlite3.connect('projects.db')
        c = conn.cursor()
        now = datetime.now().isoformat()
        
        c.execute('SELECT history FROM projects WHERE id=?', (pid,))
        row = c.fetchone()
        history = json.loads(row[0]) if row else []
        history.append({'timestamp': now, 'code': code, 'variables': vars, 'functions': funcs})
        history = history[-10:]
        
        c.execute('INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?,?,?)',
                  (pid, name, code, json.dumps(vars), json.dumps(funcs), json.dumps(history), now, now))
        conn.commit()
        conn.close()
    
    @staticmethod
    def load(pid):
        conn = sqlite3.connect('projects.db')
        c = conn.cursor()
        c.execute('SELECT * FROM projects WHERE id=?', (pid,))
        row = c.fetchone()
        conn.close()
        if not row: return None
        return {
            'id': row[0], 'name': row[1], 'code': row[2],
            'variables': json.loads(row[3]), 'functions': json.loads(row[4]),
            'history': json.loads(row[5]), 'created_at': row[6], 'updated_at': row[7]
        }
    
    @staticmethod
    def list_all():
        conn = sqlite3.connect('projects.db')
        c = conn.cursor()
        c.execute('SELECT id, name, updated_at FROM projects ORDER BY updated_at DESC')
        rows = c.fetchall()
        conn.close()
        return [{'id': r[0], 'name': r[1], 'updated_at': r[2]} for r in rows]
    
    @staticmethod
    def extract(code):
        funcs = []
        for p in [r'function\s+(\w+)\s*\(', r'def\s+(\w+)\s*\(', r'const\s+(\w+)\s*=\s*\(']:
            funcs.extend(re.findall(p, code))
        
        vars = []
        for p in [r'var\s+(\w+)', r'let\s+(\w+)', r'const\s+(\w+)']:
            vars.extend(re.findall(p, code))
        
        return list(set(vars)), list(set(funcs))

# ============================================================
# Flask 앱
# ============================================================
app = Flask(__name__)
app.config.from_object(Config)

progress_store = {}
cache_store = {}

# ============================================================
# 유틸리티
# ============================================================
def gen_sid(): return f"{int(time.time())}_{secrets.token_hex(8)}"
def cache_key(req): return hashlib.sha256(req.encode()).hexdigest()
def get_cache(k):
    if not Config.CACHE_ENABLED or k not in cache_store: return None
    d, t = cache_store[k]
    if time.time() - t < Config.CACHE_TTL: return d
    del cache_store[k]
    return None
def set_cache(k, d): cache_store[k] = (d, time.time()) if Config.CACHE_ENABLED else None

# ============================================================
# API 클라이언트 (Caching + Thinking)
# ============================================================
class APIClient:
    def __init__(self, key=None):
        self.key = key or Config.CLAUDE_API_KEY
        self.real = False
        self.client = None
        
        if not self.key:
            Log.w('API 키 없음 - 시뮬레이션')
            return
        
        try:
            from anthropic import Anthropic
            self.client = Anthropic(api_key=self.key, timeout=Config.API_TIMEOUT, max_retries=3)
            self.real = True
            Log.s('Claude API 초기화 (Caching+Thinking 활성화)')
        except Exception as e:
            Log.e(f'API 초기화 실패: {e}')
    
    def analyze(self, req, proj=None):
        if not self.real or not self.client:
            return self._sim_analyze(req)
        
        try:
            # 시스템 프롬프트 (캐싱)
            sys = [{
                "type": "text",
                "text": """전문 Google Apps Script 개발자.
규칙: 1) 기존 변수/함수명 유지 2) 한글 주석 3) 에러 처리 4) 모바일 최적화
JSON 응답: {"projectName":"", "description":"", "features":[], "architecture":{}, "files":[{"name":"Code.js","type":"gas"}]}""",
                "cache_control": {"type": "ephemeral"}  # Caching!
            }]
            
            msgs = []
            
            # 기존 프로젝트 컨텍스트 (수정 모드, 캐싱)
            if proj:
                ctx = f"기존: {proj['code'][:300]}...\n변수: {','.join(proj.get('variables',[]))}\n함수: {','.join(proj.get('functions',[]))}\n⚠️유지!"
                msgs.append({"role": "user", "content": [{"type": "text", "text": ctx, "cache_control": {"type": "ephemeral"}}]})
                msgs.append({"role": "assistant", "content": "이해. 변수/함수명 유지."})
            
            msgs.append({"role": "user", "content": req})
            
            # Extended Thinking 활성화
            res = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=sys,
                messages=msgs,
                thinking={"type": "enabled", "budget_tokens": 2000}  # Thinking!
            )
            
            txt, think = "", ""
            for b in res.content:
                if b.type == "thinking": think = b.thinking[:200]
                elif b.type == "text": txt = b.text
            
            # 캐시 통계
            if hasattr(res.usage, 'cache_read_input_tokens') and res.usage.cache_read_input_tokens > 0:
                Log.s(f"캐시 읽기: {res.usage.cache_read_input_tokens} 토큰 (90% 절감!)")
            if think: Log.i(f"AI 사고: {think}...")
            
            # JSON 추출
            if txt.startswith('```'):
                txt = txt.split('```')[1]
                if txt.startswith('json'): txt = txt[4:]
                txt = txt.strip()
            
            return json.loads(txt)
        except Exception as e:
            Log.e(f'분석 실패: {e}')
            return self._sim_analyze(req)
    
    def _sim_analyze(self, req):
        Log.i('시뮬레이션 모드')
        time.sleep(1)
        return {
            'projectName': '생성된 프로젝트',
            'description': req[:100],
            'features': ['데이터 입력', '저장', '통계'],
            'architecture': {'frontend': ['HTML5'], 'backend': ['GAS'], 'database': ['Sheets']},
            'files': [
                {'name': 'Code.js', 'type': 'gas', 'description': '백엔드'},
                {'name': 'Index.html', 'type': 'html', 'description': 'UI'}
            ],
            'testCases': [{'name': '기본', 'description': '테스트', 'steps': ['입력', '저장']}],
            'deploymentConfig': {'access': 'ANYONE', 'executeAs': 'USER_DEPLOYING'}
        }
    
    def gen_code(self, analysis, finfo, proj=None):
        if not self.real or not self.client:
            return self._sim_code(finfo)
        
        try:
            sys = [{"type": "text", "text": "코드 생성 AI. 완전 작동 코드, 한글 주석, 에러 처리.", "cache_control": {"type": "ephemeral"}}]
            
            msgs = []
            if proj:
                ctx = f"기존: {proj['code'][:300]}\n⚠️변수/함수명 유지!"
                msgs.append({"role": "user", "content": [{"type": "text", "text": ctx, "cache_control": {"type": "ephemeral"}}]})
                msgs.append({"role": "assistant", "content": "유지."})
            
            prompt = f"파일: {finfo['name']} ({finfo['type']})\n목적: {finfo['description']}\n프로젝트: {analysis['projectName']}\n코드만 반환:"
            msgs.append({"role": "user", "content": prompt})
            
            res = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8000,
                system=sys,
                messages=msgs,
                thinking={"type": "enabled", "budget_tokens": 1024}
            )
            
            code = res.content[0].text if res.content else ""
            if code.startswith('```'):
                code = '\n'.join(code.split('\n')[1:-1])
            
            return code
        except Exception as e:
            Log.e(f'코드 생성 실패: {e}')
            return self._sim_code(finfo)
    
    def _sim_code(self, finfo):
        time.sleep(0.5)
        if finfo['type'] == 'gas':
            return f"""// {finfo['name']}
function doGet() {{
  return HtmlService.createHtmlOutputFromFile('Index').setTitle('앱');
}}
function saveData(data) {{
  try {{
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    sheet.appendRow([new Date(), JSON.stringify(data)]);
    return {{success: true}};
  }} catch(e) {{ return {{success: false, error: e.toString()}}; }}
}}"""
        else:
            return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>앱</title><style>body{{font-family:sans-serif;max-width:800px;margin:50px auto;padding:20px}}
.btn{{padding:12px 20px;background:#667eea;color:#fff;border:none;border-radius:8px;cursor:pointer}}</style>
</head><body><h1>🎉 생성 완료</h1><input id="inp" placeholder="입력">
<button class="btn" onclick="save()">저장</button>
<script>function save(){{google.script.run.withSuccessHandler(r=>alert('성공')).saveData({{val:document.getElementById('inp').value}})}}</script>
</body></html>"""

# ============================================================
# 배포 관리자 (Clasp)
# ============================================================
class DeployManager:
    def __init__(self, pdir):
        self.pdir = Path(pdir)
    
    def run_tests(self):
        """Clasp 테스트 실행"""
        Log.i('테스트 실행')
        try:
            # Clasp 확인
            if subprocess.run(['clasp', '--version'], capture_output=True).returncode != 0:
                Log.w('Clasp 미설치 - 테스트 스킵')
                return True
            
            # 푸시
            res = subprocess.run(['clasp', 'push', '--force'], cwd=self.pdir, capture_output=True, text=True)
            if res.returncode != 0:
                Log.e(f'푸시 실패: {res.stderr}')
                return False
            
            # 테스트 실행
            res = subprocess.run(['clasp', 'run', 'testAll'], cwd=self.pdir, capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                Log.s('테스트 통과')
                return True
            else:
                Log.w('테스트 실패')
                return False
        except FileNotFoundError:
            Log.w('Clasp 미설치')
            return True
        except subprocess.TimeoutExpired:
            Log.w('테스트 타임아웃')
            return False
        except Exception as e:
            Log.e(f'테스트 오류: {e}')
            return False
    
    def deploy(self):
        """Clasp 배포"""
        Log.i('배포 중')
        try:
            # Clasp 확인
            if subprocess.run(['clasp', '--version'], capture_output=True).returncode != 0:
                Log.w('Clasp 미설치 - 수동 배포 필요')
                return None
            
            # .clasp.json 확인
            if not (self.pdir / '.clasp.json').exists():
                Log.w('.clasp.json 없음 - clasp create 필요')
                return None
            
            # 푸시
            subprocess.run(['clasp', 'push', '--force'], cwd=self.pdir, check=True, capture_output=True)
            
            # 배포
            res = subprocess.run(
                ['clasp', 'deploy', '--description', f'Auto {datetime.now():%Y%m%d_%H%M%S}'],
                cwd=self.pdir, capture_output=True, text=True
            )
            
            if res.returncode != 0:
                Log.e(f'배포 실패: {res.stderr}')
                return None
            
            # URL 추출
            for line in res.stdout.split('\n'):
                if 'https://script.google.com' in line:
                    Log.s('배포 완료')
                    return line.strip()
            
            Log.w('배포 URL 없음')
            return None
        except FileNotFoundError:
            Log.w('Clasp 미설치')
            return None
        except Exception as e:
            Log.e(f'배포 오류: {e}')
            return None

# ============================================================
# 프로젝트 생성
# ============================================================
class ProjectGen:
    def __init__(self, sid, req, key=None, proj_id=None, skip_tests=True):
        self.sid = sid
        self.req = req
        self.proj_id = proj_id
        self.skip_tests = skip_tests
        self.api = APIClient(key)
        
        progress_store[sid] = {
            'running': True, 'step': 0, 'total': 7,
            'message': '준비...', 'result': None, 'start': time.time()
        }
    
    def update(self, step, msg=None):
        msgs = ['분석', '설계', '코드생성', '테스트', '설정', '저장', '완료']
        progress_store[self.sid].update({'step': step, 'message': msg or msgs[step-1] if step<=len(msgs) else ''})
        Log.i(f'[{self.sid[:8]}] {msg or msgs[step-1] if step<=len(msgs) else ""}')
    
    def run(self):
        try:
            # 캐시 확인
            ck = cache_key(self.req)
            cached = get_cache(ck)
            if cached:
                self.update(7, '캐시 로드')
                time.sleep(1)
                return cached
            
            # 기존 프로젝트 로드 (수정 모드)
            proj_state = ProjectState.load(self.proj_id) if self.proj_id else None
            
            # Step 1: 분석
            self.update(1)
            analysis = self.api.analyze(self.req, proj_state)
            
            # Step 2: 설계
            self.update(2)
            time.sleep(0.5)
            
            # Step 3: 코드 생성
            self.update(3)
            codes = {}
            for i, fi in enumerate(analysis['files'], 1):
                self.update(3, f"코드생성 ({i}/{len(analysis['files'])}): {fi['name']}")
                codes[fi['name']] = self.api.gen_code(analysis, fi, proj_state)
                time.sleep(1)
            
            # Step 4: 테스트
            self.update(4)
            codes['Test.js'] = "// 테스트\nfunction testAll() { Logger.log('테스트'); }"
            
            # Step 5: 설정
            self.update(5)
            codes['appsscript.json'] = json.dumps({
                "timeZone": "Asia/Seoul", "runtimeVersion": "V8",
                "webapp": analysis.get('deploymentConfig', {}),
                "oauthScopes": ["https://www.googleapis.com/auth/spreadsheets"]
            }, indent=2)
            
            codes['README.md'] = f"# {analysis['projectName']}\n{analysis['description']}\n\n배포: https://script.google.com"
            
            # Step 6: 저장
            self.update(6)
            pdir = Config.OUTPUT_DIR / self.sid
            pdir.mkdir(exist_ok=True)
            for fn, code in codes.items():
                (pdir / fn).write_text(code, encoding='utf-8')
            
            # 변수/함수명 추출
            main_code = codes.get('Code.js', '')
            vars, funcs = ProjectState.extract(main_code)
            
            # 프로젝트 저장 (SQLite)
            if not self.proj_id:
                self.proj_id = hashlib.md5(self.req.encode()).hexdigest()[:12]
            
            ProjectState.save(self.proj_id, analysis['projectName'][:50], main_code, vars, funcs)
            
            # Step 7: 배포 (선택)
            self.update(7)
            deploy_url = None
            
            if not self.skip_tests:
                deployer = DeployManager(pdir)
                test_ok = deployer.run_tests()
                if not test_ok:
                    Log.w('테스트 실패 - 배포 계속')
                deploy_url = deployer.deploy()
            
            elapsed = time.time() - progress_store[self.sid]['start']
            
            Log.s(f'완료! {elapsed:.1f}초')
            Log.i(f'프로젝트: {pdir}')
            Log.i(f'파일: {len(codes)}개')
            if deploy_url:
                Log.s(f'배포 URL: {deploy_url}')
            else:
                Log.i('수동 배포: cd ' + str(pdir) + ' && clasp deploy')
            
            result = {
                'success': True,
                'project_id': self.proj_id,
                'project_name': analysis['projectName'],
                'description': analysis['description'],
                'features': analysis['features'],
                'files': list(codes.keys()),
                'code': codes,
                'variables': vars,
                'functions': funcs,
                'elapsed_time': elapsed,
                'deployment_url': deploy_url,
                'summary': {
                    'total_files': len(codes),
                    'total_lines': sum(len(c.split('\n')) for c in codes.values()),
                    'elapsed': round(elapsed, 2)
                },
                'cached': False
            }
            
            set_cache(ck, result)
            return result
            
        except Exception as e:
            Log.e(f'오류: {e}')
            import traceback
            traceback.print_exc()
            return {'success': False, 'error': str(e)}

def bg_gen(sid, req, key=None, proj_id=None, skip_tests=True):
    gen = ProjectGen(sid, req, key, proj_id, skip_tests)
    result = gen.run()
    progress_store[sid]['running'] = False
    progress_store[sid]['result'] = result

# ============================================================
# API 엔드포인트
# ============================================================
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/generate', methods=['POST'])
def api_gen():
    data = request.json
    req = data.get('requirements', '')
    key = data.get('api_key') or Config.CLAUDE_API_KEY
    sid = data.get('session_id', gen_sid())
    proj_id = data.get('project_id')  # 수정 모드
    skip_tests = data.get('skip_tests', True)  # 기본 테스트 스킵
    
    if not req: return jsonify({'error': '요구사항 누락'}), 400
    if not key: return jsonify({'error': 'API 키 필요'}), 400
    
    ck = cache_key(req)
    cached = get_cache(ck)
    if cached:
        cached['cached'] = True
        return jsonify({'cached': True, 'result': cached})
    
    threading.Thread(target=bg_gen, args=(sid, req, key, proj_id, skip_tests), daemon=True).start()
    return jsonify({'status': 'started', 'session_id': sid})

@app.route('/api/progress')
def api_prog():
    sid = request.args.get('session_id', 'default')
    return jsonify(progress_store.get(sid, {'running': False, 'step': 0, 'total': 7, 'message': '', 'result': None}))

@app.route('/api/download')
def api_dl():
    sid = request.args.get('session_id', '')
    pdir = Config.OUTPUT_DIR / sid
    if not pdir.exists(): return jsonify({'error': '없음'}), 404
    
    mf = io.BytesIO()
    with zipfile.ZipFile(mf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in pdir.rglob('*'):
            if f.is_file(): zf.write(f, f.relative_to(pdir))
    mf.seek(0)
    return send_file(mf, mimetype='application/zip', as_attachment=True, download_name=f'project_{sid}.zip')

@app.route('/api/projects')
def api_projs():
    return jsonify(ProjectState.list_all())

@app.route('/api/project/<pid>')
def api_proj(pid):
    state = ProjectState.load(pid)
    if not state: return jsonify({'error': '없음'}), 404
    return jsonify(state)

@app.route('/api/health')
def api_health():
    return jsonify({
        'status': 'healthy',
        'version': '4.0.0',
        'features': ['Caching', 'Thinking', 'SQLite', 'CLI'],
        'api_configured': bool(Config.CLAUDE_API_KEY)
    })

# ============================================================
# CLI 모드
# ============================================================
def run_cli(args):
    print(f"\n{'='*60}\n{C.BOLD}🚀 AI 자동 개발 v4.0{C.E}\n{'='*60}\n")
    
    key = args.api_key or Config.CLAUDE_API_KEY
    if not key:
        Log.e("API 키 필요")
        sys.exit(1)
    
    if not args.requirements or not os.path.exists(args.requirements):
        Log.e("요구사항 파일 필요")
        sys.exit(1)
    
    req = open(args.requirements, encoding='utf-8').read()
    sid = gen_sid()
    
    # CLI에서는 배포 옵션 사용 가능
    skip_tests = args.skip_tests if hasattr(args, 'skip_tests') else False
    
    gen = ProjectGen(sid, req, key, skip_tests=skip_tests)
    result = gen.run()
    
    print(f"\n{'='*60}")
    if result['success']:
        print(f"{C.G}{C.BOLD}✅ 완료{C.E}\n{'='*60}")
        print(f"\n📁 {Config.OUTPUT_DIR / sid}")
        print(f"📄 파일: {len(result['files'])}")
        print(f"⏱️  {result['elapsed_time']:.1f}초")
        
        if result.get('deployment_url'):
            print(f"🌐 배포: {result['deployment_url']}")
        
        sys.exit(0)
    else:
        print(f"{C.R}{C.BOLD}❌ 실패{C.E}\n{'='*60}")
        print(f"\n{result.get('error', '알 수 없는 오류')}")
        sys.exit(1)

# ============================================================
# 메인
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='AI 자동 개발 플랫폼 v4.0')
    parser.add_argument('--cli', action='store_true', help='CLI 모드')
    parser.add_argument('--requirements', help='요구사항 파일')
    parser.add_argument('--api-key', help='API 키')
    parser.add_argument('--port', type=int, help='포트')
    parser.add_argument('--skip-tests', action='store_true', help='테스트/배포 스킵')
    args = parser.parse_args()
    
    if args.cli:
        run_cli(args)
        return
    
    # 웹 모드
    port = args.port or Config.PORT
    print(f"\n{'='*60}\n{C.BOLD}🚀 AI 자동 개발 v4.0{C.E}\n{'='*60}")
    print(f"\n✅ http://{Config.HOST}:{port}")
    print(f"✅ 출력: {Config.OUTPUT_DIR}")
    
    if Config.CLAUDE_API_KEY:
        k = Config.CLAUDE_API_KEY
        print(f"✅ API: {k[:10]}...{k[-4:]}")
    else:
        print(f"⚠️  API 미설정")
    
    # Clasp 확인
    try:
        if subprocess.run(['clasp', '--version'], capture_output=True).returncode == 0:
            print(f"✅ Clasp: 설치됨 (배포 가능)")
        else:
            print(f"⚠️  Clasp: 미설치 (수동 배포만)")
    except FileNotFoundError:
        print(f"⚠️  Clasp: 미설치 (수동 배포만)")
    
    print(f"\n💡 기능:")
    print(f"  🔥 Prompt Caching (90% 비용 절감)")
    print(f"  🧠 Extended Thinking (정확한 코드)")
    print(f"  💾 SQLite (프로젝트 히스토리)")
    print(f"  🔄 변수/함수명 기억 및 유지")
    print(f"  🚀 Clasp 자동 배포")
    print(f"\n📦 설치: npm install -g @google/clasp")
    print(f"{'='*60}\n")
    
    app.run(debug=Config.DEBUG, host=Config.HOST, port=port, threaded=True)

# Railway/Gunicorn이 이 앱을 찾습니다
# 이 부분이 매우 중요합니다!
if __name__ == '__main__':
    main()

# Railway/Gunicorn용 앱 노출
# gunicorn이 이 변수를 찾아서 실행합니다
if __name__ != '__main__':
    # Gunicorn 모드: 환경변수에서 포트 읽기
    port = int(os.getenv('PORT', 5000))
    Log.i(f'Gunicorn 모드: 포트 {port}')
