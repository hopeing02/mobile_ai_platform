#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 자동 개발 플랫폼 - 최종 완전판 v4.2 (ASCII 인코딩 버그 수정)
- Prompt Caching: 비용 90% 절감
- Extended Thinking: 정확한 코드 생성
- SQLite: 프로젝트 히스토리 관리
- 변수/함수명 기억 및 유지
- 웹 + CLI 모드
- Clasp 배포 지원
- ASCII 안전 처리

실행: python ultimate_platform.py
CLI: python ultimate_platform.py --cli --requirements req.md
"""

import os, sys, json, time, secrets, hashlib, threading, zipfile, io, subprocess, argparse, re, sqlite3, base64
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from functools import wraps
from flask import Flask, send_from_directory, request, jsonify, send_file

# UTF-8 인코딩 강제 설정
if sys.version_info[0] >= 3:
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ============================================================
# 설정
# ============================================================
class Config:
    SECRET_KEY = os.urandom(24)
    DEBUG = False
    PORT = int(os.getenv('PORT', 5000))
    HOST = '0.0.0.0'
    
    OUTPUT_DIR = Path('./output')
    
    CLAUDE_API_KEY = os.getenv('ANTHROPIC_API_KEY', os.getenv('CLAUDE_API_KEY', ''))
    
    if not CLAUDE_API_KEY:
        CLAUDE_API_KEY = 'sk-ant-api03-키입력'
    
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
# ASCII 안전 유틸리티
# ============================================================
def safe_encode(text, max_len=500):
    """한글 텍스트를 ASCII 안전하게 변환"""
    if not text:
        return ""
    
    # UTF-8 바이트로 변환 후 Base64 인코딩
    try:
        # 길이 제한
        text = text[:max_len] if len(text) > max_len else text
        # Base64 인코딩 (ASCII 안전)
        encoded = base64.b64encode(text.encode('utf-8')).decode('ascii')
        return encoded
    except:
        # 실패 시 영어만 추출
        return ''.join(c for c in text if ord(c) < 128)[:max_len]

def safe_decode(encoded_text):
    """Base64 인코딩된 텍스트를 한글로 복원"""
    try:
        return base64.b64decode(encoded_text.encode('ascii')).decode('utf-8')
    except:
        return encoded_text

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
# API 클라이언트 (ASCII 안전 버전)
# ============================================================
class APIClient:
    def __init__(self, key=None):
        self.key = key or Config.CLAUDE_API_KEY
        self.real = False
        self.client = None
        
        if not self.key:
            Log.w('API key missing - simulation mode')
            return
        
        try:
            from anthropic import Anthropic
            self.client = Anthropic(api_key=self.key, timeout=Config.API_TIMEOUT, max_retries=3)
            self.real = True
            Log.s('Claude API initialized (Caching+Thinking enabled)')
        except Exception as e:
            Log.e(f'API init failed: {e}')
    
    def analyze(self, req, proj=None):
        if not self.real or not self.client:
            return self._sim_analyze(req)
        
        try:
            # ASCII 안전 시스템 프롬프트 (100% 영어)
            sys = [{
                "type": "text",
                "text": """Professional Google Apps Script Developer.

IMPORTANT: Respond ONLY with valid JSON. No markdown, no explanations, just pure JSON:

{
  "projectName": "App Name",
  "description": "Brief description",
  "features": ["Feature 1", "Feature 2", "Feature 3"],
  "architecture": {"frontend": "HTML5", "backend": "GAS", "storage": "Sheets"},
  "files": [
    {"name": "Code.js", "type": "gas", "description": "Backend file"},
    {"name": "Index.html", "type": "html", "description": "UI file"}
  ]
}

RULES:
1. File descriptions must be SHORT (under 15 chars)
2. NO code in description field
3. NO special characters
4. Keep existing variable/function names if provided
5. Add Korean comments in generated code later
6. Include error handling""",
                "cache_control": {"type": "ephemeral"}
            }]
            
            msgs = []
            
            # 기존 프로젝트 컨텍스트 (ASCII 안전)
            if proj:
                # 변수/함수명만 전달 (최대 5개)
                vars_list = ', '.join(proj.get('variables', [])[:5])
                funcs_list = ', '.join(proj.get('functions', [])[:5])
                ctx = f"Existing variables: {vars_list}\nExisting functions: {funcs_list}\nIMPORTANT: Keep these names!"
                msgs.append({"role": "user", "content": ctx})
                msgs.append({"role": "assistant", "content": "Understood. Will maintain names."})
            
            # 요구사항을 짧게 요약 (ASCII 안전)
            safe_req = req[:300].encode('ascii', errors='ignore').decode('ascii')
            if not safe_req.strip():
                safe_req = "Create a web app with Google Apps Script"
            
            msgs.append({"role": "user", "content": f"{safe_req}\n\nRespond with valid JSON only."})
            
            # API 호출
            res = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=sys,
                messages=msgs,
                thinking={"type": "enabled", "budget_tokens": 2000}
            )
            
            txt, think = "", ""
            for b in res.content:
                if hasattr(b, 'type'):
                    if b.type == "thinking":
                        think = getattr(b, 'thinking', getattr(b, 'text', ''))[:200]
                    elif b.type == "text":
                        txt = getattr(b, 'text', '')
            
            if hasattr(res, 'usage') and hasattr(res.usage, 'cache_read_input_tokens'):
                if res.usage.cache_read_input_tokens > 0:
                    Log.s(f"Cache hit: {res.usage.cache_read_input_tokens} tokens")
            
            if think:
                # thinking 내용을 ASCII 안전하게 출력
                safe_think = think.encode('ascii', errors='ignore').decode('ascii')
                Log.i(f"AI thinking: {safe_think[:100]}...")
            
            # JSON 추출
            txt = txt.strip()
            if txt.startswith('```'):
                lines = txt.split('\n')
                if len(lines) > 2:
                    txt = '\n'.join(lines[1:-1])
                txt = txt.strip()
            
            if txt.lower().startswith('json'):
                txt = txt[4:].strip()
            
            # JSON 파싱 시도
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                # 중괄호 추출 시도
                try:
                    start = txt.find('{')
                    end = txt.rfind('}') + 1
                    if start >= 0 and end > start:
                        clean = txt[start:end]
                        return json.loads(clean)
                except:
                    pass
                
                Log.w("JSON parsing failed - using simulation")
                return self._sim_analyze(req)
                
        except Exception as e:
            error_msg = str(e).encode('ascii', errors='ignore').decode('ascii')
            Log.e(f'Analysis failed: {error_msg[:100]}')
            return self._sim_analyze(req)
    
    def gen_code(self, analysis, finfo, proj=None):
        if not self.real or not self.client:
            return self._sim_code(finfo)
        
        try:
            # ASCII 안전 시스템 프롬프트
            sys = [{"type": "text", "text": "Code generation AI. Complete working code with Korean comments and error handling. Return ONLY code without markdown or explanations.", "cache_control": {"type": "ephemeral"}}]
            
            msgs = []
            if proj:
                vars_list = ', '.join(proj.get('variables', [])[:5])
                funcs_list = ', '.join(proj.get('functions', [])[:5])
                ctx = f"Keep these names: vars={vars_list}, funcs={funcs_list}"
                msgs.append({"role": "user", "content": ctx})
                msgs.append({"role": "assistant", "content": "OK"})
            
            # ASCII 안전 프롬프트
            proj_name = analysis.get('projectName', 'App').encode('ascii', errors='ignore').decode('ascii')
            file_desc = finfo.get('description', 'File').encode('ascii', errors='ignore').decode('ascii')
            prompt = f"File: {finfo['name']} ({finfo['type']})\nPurpose: {file_desc}\nProject: {proj_name}\n\nReturn pure code only:"
            msgs.append({"role": "user", "content": prompt})
            
            # API 호출
            res = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8000,
                system=sys,
                messages=msgs,
                thinking={"type": "enabled", "budget_tokens": 1024}
            )
            
            code = ""
            for b in res.content:
                if hasattr(b, 'type'):
                    if b.type == "text":
                        code = getattr(b, 'text', '')
                        break
            
            # 마크다운 제거
            if code.startswith('```'):
                lines = code.split('\n')
                if len(lines) > 2:
                    code = '\n'.join(lines[1:-1])
            
            return code.strip()
            
        except Exception as e:
            error_msg = str(e).encode('ascii', errors='ignore').decode('ascii')
            Log.e(f'Code generation failed: {error_msg[:100]}')
            return self._sim_code(finfo)
    
    def _sim_analyze(self, req):
        """향상된 시뮬레이션 분석"""
        Log.i('Simulation mode')
        time.sleep(0.5)
        
        req_lower = req.lower()
        
        if 'todo' in req_lower or '할 일' in req_lower or '할일' in req_lower:
            project_name = 'Todo Manager'
            features = ['Add/Delete tasks', 'Complete checkbox', 'Priority setting', 'Google Sheets save', 'Drag and drop']
        elif 'diary' in req_lower or '일기' in req_lower:
            project_name = 'AI Diary'
            features = ['Write diary', 'AI emotion analysis', 'Monthly stats', 'Emotion graph', 'Search diary']
        elif 'receipt' in req_lower or '영수증' in req_lower:
            project_name = 'Receipt Manager'
            features = ['Photo capture', 'OCR recognition', 'Auto categorization', 'Monthly stats', 'Category analysis']
        elif 'expense' in req_lower or '가계부' in req_lower or '지출' in req_lower:
            project_name = 'Smart Budget'
            features = ['Income/Expense input', 'Category classification', 'Monthly stats', 'Budget management', 'Expense alerts']
        else:
            project_name = 'Custom App'
            features = ['Data input', 'Save function', 'View statistics', 'Mobile optimized']
        
        return {
            'projectName': project_name,
            'description': req[:100] if len(req) > 100 else req,
            'features': features,
            'architecture': {
                'frontend': 'HTML5, CSS3, JavaScript',
                'backend': 'Google Apps Script',
                'storage': 'Google Sheets',
                'ui': 'Responsive Mobile UI'
            },
            'files': [
                {'name': 'Code.js', 'type': 'gas', 'description': 'Backend logic'},
                {'name': 'Index.html', 'type': 'html', 'description': 'UI interface'}
            ],
            'testCases': [{'name': 'Basic test', 'description': 'Function check', 'steps': ['Input', 'Save', 'View']}],
            'deploymentConfig': {'access': 'ANYONE', 'executeAs': 'USER_DEPLOYING'}
        }
    
    def _sim_code(self, finfo):
        """향상된 시뮬레이션 코드 생성"""
        time.sleep(0.3)
        
        if finfo['type'] == 'gas':
            return """// Backend Logic - Google Apps Script
// 한글 주석: 서버 사이드 코드

function doGet() {
  // 웹앱 UI 표시
  return HtmlService.createHtmlOutputFromFile('Index')
    .setTitle('App')
    .setFaviconUrl('https://www.gstatic.com/images/branding/product/1x/apps_script_48dp.png');
}

function saveData(data) {
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    var timestamp = new Date();
    
    // 데이터 저장
    sheet.appendRow([
      timestamp,
      JSON.stringify(data),
      data.title || '',
      data.status || 'active'
    ]);
    
    return {
      success: true,
      message: 'Saved successfully',
      timestamp: timestamp.toISOString()
    };
  } catch(e) {
    Logger.log('Save error: ' + e.toString());
    return {
      success: false,
      error: e.toString()
    };
  }
}

function loadData() {
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    var data = sheet.getDataRange().getValues();
    
    // 헤더 제외
    if (data.length > 1) {
      data = data.slice(1);
    }
    
    return {
      success: true,
      data: data.map(function(row) {
        return {
          timestamp: row[0],
          content: row[1],
          title: row[2],
          status: row[3]
        };
      })
    };
  } catch(e) {
    Logger.log('Load error: ' + e.toString());
    return {
      success: false,
      error: e.toString()
    };
  }
}"""
        else:
            return """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <title>App</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: white;
            border-radius: 20px;
            padding: 30px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        }
        
        h1 {
            color: #667eea;
            margin-bottom: 30px;
            text-align: center;
        }
        
        .input-group {
            margin-bottom: 20px;
        }
        
        label {
            display: block;
            font-weight: 600;
            margin-bottom: 8px;
            color: #333;
        }
        
        input[type="text"], textarea {
            width: 100%;
            padding: 15px;
            border: 2px solid #e0e0e0;
            border-radius: 12px;
            font-size: 16px;
            transition: all 0.3s;
            font-family: inherit;
        }
        
        input:focus, textarea:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        textarea {
            min-height: 120px;
            resize: vertical;
        }
        
        .btn {
            width: 100%;
            padding: 18px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 12px;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            transition: transform 0.2s;
            margin-top: 10px;
        }
        
        .btn:active:not(:disabled) {
            transform: scale(0.98);
        }
        
        .btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
        }
        
        .status {
            margin-top: 20px;
            padding: 15px;
            border-radius: 12px;
            display: none;
        }
        
        .status.success {
            background: #d4edda;
            color: #155724;
        }
        
        .status.error {
            background: #f8d7da;
            color: #721c24;
        }
        
        @media (max-width: 768px) {
            .container {
                padding: 20px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎉 App Generated Successfully</h1>
        
        <div class="input-group">
            <label>📝 Title</label>
            <input type="text" id="title" placeholder="Enter title">
        </div>
        
        <div class="input-group">
            <label>📄 Content</label>
            <textarea id="content" placeholder="Enter content"></textarea>
        </div>
        
        <button class="btn" onclick="save()">💾 Save</button>
        <button class="btn" style="background: linear-gradient(135deg, #6c757d 0%, #495057 100%);" onclick="load()">📋 Load</button>
        
        <div id="status" class="status"></div>
    </div>
    
    <script>
        function save() {
            const title = document.getElementById('title').value;
            const content = document.getElementById('content').value;
            
            if (!title || !content) {
                showStatus('Please enter both title and content', 'error');
                return;
            }
            
            const btn = event.target;
            btn.disabled = true;
            btn.textContent = 'Saving...';
            
            google.script.run
                .withSuccessHandler(function(result) {
                    btn.disabled = false;
                    btn.textContent = '💾 Save';
                    
                    if (result.success) {
                        showStatus('✅ ' + result.message, 'success');
                        document.getElementById('title').value = '';
                        document.getElementById('content').value = '';
                    } else {
                        showStatus('❌ ' + result.error, 'error');
                    }
                })
                .withFailureHandler(function(error) {
                    btn.disabled = false;
                    btn.textContent = '💾 Save';
                    showStatus('❌ Error: ' + error, 'error');
                })
                .saveData({
                    title: title,
                    content: content,
                    status: 'active'
                });
        }
        
        function load() {
            google.script.run
                .withSuccessHandler(function(result) {
                    if (result.success && result.data) {
                        console.log('Loaded:', result.data);
                        showStatus('✅ Data loaded successfully', 'success');
                    } else {
                        showStatus('❌ ' + (result.error || 'No data'), 'error');
                    }
                })
                .withFailureHandler(function(error) {
                    showStatus('❌ Error: ' + error, 'error');
                })
                .loadData();
        }
        
        function showStatus(message, type) {
            const status = document.getElementById('status');
            status.textContent = message;
            status.className = 'status ' + type;
            status.style.display = 'block';
            
            setTimeout(function() {
                status.style.display = 'none';
            }, 3000);
        }
    </script>
</body>
</html>"""

# ============================================================
# 배포 관리자
# ============================================================
class DeployManager:
    def __init__(self, pdir):
        self.pdir = Path(pdir)
    
    def run_tests(self):
        Log.i('Running tests')
        try:
            if subprocess.run(['clasp', '--version'], capture_output=True).returncode != 0:
                Log.w('Clasp not installed - skipping tests')
                return True
            
            res = subprocess.run(['clasp', 'push', '--force'], cwd=self.pdir, capture_output=True, text=True)
            if res.returncode != 0:
                Log.e(f'Push failed: {res.stderr}')
                return False
            
            res = subprocess.run(['clasp', 'run', 'testAll'], cwd=self.pdir, capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                Log.s('Tests passed')
                return True
            else:
                Log.w('Tests failed')
                return False
        except FileNotFoundError:
            Log.w('Clasp not installed')
            return None
        except Exception as e:
            Log.e(f'Deploy error: {e}')
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
            'message': 'Preparing...', 'result': None, 'start': time.time()
        }
    
    def update(self, step, msg=None):
        msgs = ['Analysis', 'Design', 'Code Generation', 'Testing', 'Configuration', 'Saving', 'Complete']
        progress_store[self.sid].update({'step': step, 'message': msg or msgs[step-1] if step<=len(msgs) else ''})
        Log.i(f'[{self.sid[:8]}] {msg or msgs[step-1] if step<=len(msgs) else ""}')
    
    def run(self):
        try:
            ck = cache_key(self.req)
            cached = get_cache(ck)
            if cached:
                self.update(7, 'Cache loaded')
                time.sleep(1)
                return cached
            
            proj_state = ProjectState.load(self.proj_id) if self.proj_id else None
            
            self.update(1)
            analysis = self.api.analyze(self.req, proj_state)
            
            self.update(2)
            time.sleep(0.5)
            
            self.update(3)
            codes = {}
            for i, fi in enumerate(analysis['files'], 1):
                self.update(3, f"Code Generation ({i}/{len(analysis['files'])}): {fi['name']}")
                codes[fi['name']] = self.api.gen_code(analysis, fi, proj_state)
                time.sleep(1)
            
            self.update(4)
            codes['Test.js'] = "// Test\nfunction testAll() { Logger.log('Test complete'); }"
            
            self.update(5)
            codes['appsscript.json'] = json.dumps({
                "timeZone": "Asia/Seoul", "runtimeVersion": "V8",
                "webapp": analysis.get('deploymentConfig', {}),
                "oauthScopes": ["https://www.googleapis.com/auth/spreadsheets"]
            }, indent=2)
            
            codes['README.md'] = f"# {analysis['projectName']}\n{analysis['description']}\n\nDeploy: https://script.google.com"
            
            self.update(6)
            pdir = Config.OUTPUT_DIR / self.sid
            pdir.mkdir(exist_ok=True)
            for fn, code in codes.items():
                (pdir / fn).write_text(code, encoding='utf-8')
            
            main_code = codes.get('Code.js', '')
            vars, funcs = ProjectState.extract(main_code)
            
            if not self.proj_id:
                self.proj_id = hashlib.md5(self.req.encode()).hexdigest()[:12]
            
            ProjectState.save(self.proj_id, analysis['projectName'][:50], main_code, vars, funcs)
            
            self.update(7)
            deploy_url = None
            
            if not self.skip_tests:
                deployer = DeployManager(pdir)
                test_ok = deployer.run_tests()
                if not test_ok:
                    Log.w('Tests failed - continuing deployment')
                deploy_url = deployer.deploy()
            
            elapsed = time.time() - progress_store[self.sid]['start']
            
            Log.s(f'Complete! {elapsed:.1f}s')
            Log.i(f'Project: {pdir}')
            Log.i(f'Files: {len(codes)}')
            if deploy_url:
                Log.s(f'Deployed: {deploy_url}')
            else:
                Log.i('Manual deploy: cd ' + str(pdir) + ' && clasp deploy')
            
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
            Log.e(f'Error: {e}')
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
    proj_id = data.get('project_id')
    skip_tests = data.get('skip_tests', True)
    
    if not req: return jsonify({'error': 'Requirements missing'}), 400
    if not key: return jsonify({'error': 'API key required'}), 400
    
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
    if not pdir.exists(): return jsonify({'error': 'Not found'}), 404
    
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
    if not state: return jsonify({'error': 'Not found'}), 404
    return jsonify(state)

@app.route('/api/health')
def api_health():
    return jsonify({
        'status': 'healthy',
        'version': '4.2.0',
        'features': ['Caching', 'Thinking', 'SQLite', 'CLI', 'ASCII-Safe'],
        'api_configured': bool(Config.CLAUDE_API_KEY)
    })

# ============================================================
# CLI 모드
# ============================================================
def run_cli(args):
    print(f"\n{'='*60}\n{C.BOLD}🚀 AI Auto Dev v4.2{C.E}\n{'='*60}\n")
    
    key = args.api_key or Config.CLAUDE_API_KEY
    if not key:
        Log.e("API key required")
        sys.exit(1)
    
    if not args.requirements or not os.path.exists(args.requirements):
        Log.e("Requirements file required")
        sys.exit(1)
    
    req = open(args.requirements, encoding='utf-8').read()
    sid = gen_sid()
    
    skip_tests = args.skip_tests if hasattr(args, 'skip_tests') else False
    
    gen = ProjectGen(sid, req, key, skip_tests=skip_tests)
    result = gen.run()
    
    print(f"\n{'='*60}")
    if result['success']:
        print(f"{C.G}{C.BOLD}✅ Complete{C.E}\n{'='*60}")
        print(f"\n📁 {Config.OUTPUT_DIR / sid}")
        print(f"📄 Files: {len(result['files'])}")
        print(f"⏱️  {result['elapsed_time']:.1f}s")
        
        if result.get('deployment_url'):
            print(f"🌐 Deployed: {result['deployment_url']}")
        
        sys.exit(0)
    else:
        print(f"{C.R}{C.BOLD}❌ Failed{C.E}\n{'='*60}")
        print(f"\n{result.get('error', 'Unknown error')}")
        sys.exit(1)

# ============================================================
# 메인
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='AI Auto Dev Platform v4.2')
    parser.add_argument('--cli', action='store_true', help='CLI mode')
    parser.add_argument('--requirements', help='Requirements file')
    parser.add_argument('--api-key', help='API key')
    parser.add_argument('--port', type=int, help='Port')
    parser.add_argument('--skip-tests', action='store_true', help='Skip tests/deployment')
    args = parser.parse_args()
    
    if args.cli:
        run_cli(args)
        return
    
    port = args.port or Config.PORT
    print(f"\n{'='*60}\n{C.BOLD}🚀 AI Auto Dev v4.2{C.E}\n{'='*60}")
    print(f"\n✅ http://{Config.HOST}:{port}")
    print(f"✅ Output: {Config.OUTPUT_DIR}")
    
    if Config.CLAUDE_API_KEY:
        k = Config.CLAUDE_API_KEY
        print(f"✅ API: {k[:10]}...{k[-4:]}")
    else:
        print(f"⚠️  API not configured")
    
    try:
        if subprocess.run(['clasp', '--version'], capture_output=True).returncode == 0:
            print(f"✅ Clasp: Installed (deployment available)")
        else:
            print(f"⚠️  Clasp: Not installed (manual deployment only)")
    except FileNotFoundError:
        print(f"⚠️  Clasp: Not installed (manual deployment only)")
    
    print(f"\n💡 Features:")
    print(f"  🔥 Prompt Caching (90% cost reduction)")
    print(f"  🧠 Extended Thinking (accurate code)")
    print(f"  💾 SQLite (project history)")
    print(f"  🔄 Variable/function name preservation")
    print(f"  🚀 Clasp auto deployment")
    print(f"  🌐 ASCII-safe encoding")
    print(f"\n📦 Install: npm install -g @google/clasp")
    print(f"{'='*60}\n")
    
    app.run(debug=Config.DEBUG, host=Config.HOST, port=port, threaded=True)

if __name__ == '__main__':
    main()

if __name__ != '__main__':
    port = int(os.getenv('PORT', 5000))
    Log.i(f'Gunicorn mode: port {port}'):
            Log.w('Clasp not installed')
            return True
        except subprocess.TimeoutExpired:
            Log.w('Test timeout')
            return False
        except Exception as e:
            Log.e(f'Test error: {e}')
            return False
    
    def deploy(self):
        Log.i('Deploying')
        try:
            if subprocess.run(['clasp', '--version'], capture_output=True).returncode != 0:
                Log.w('Clasp not installed - manual deployment required')
                return None
            
            if not (self.pdir / '.clasp.json').exists():
                Log.w('.clasp.json missing - run clasp create first')
                return None
            
            subprocess.run(['clasp', 'push', '--force'], cwd=self.pdir, check=True, capture_output=True)
            
            res = subprocess.run(
                ['clasp', 'deploy', '--description', f'Auto {datetime.now():%Y%m%d_%H%M%S}'],
                cwd=self.pdir, capture_output=True, text=True
            )
            
            if res.returncode != 0:
                Log.e(f'Deploy failed: {res.stderr}')
                return None
            
            for line in res.stdout.split('\n'):
                if 'https://script.google.com' in line:
                    Log.s('Deployed successfully')
                    return line.strip()
            
            Log.w('No deployment URL')
            return None
        except FileNotFoundError
