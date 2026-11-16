import os
import uvicorn
from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import List, Optional
import secrets
from core import (
    MikroTikManager,
    load_targets,
    save_targets,
    get_config_env,
    save_config_env,
    logger
)

app = FastAPI(title="MikroTik Stream Router", version="2.0.0")
security = HTTPBasic(auto_error=False)

def check_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    cfg = get_config_env()
    req_user = cfg.get("WEB_USERNAME")
    req_pass = cfg.get("WEB_PASSWORD")
    
    # If no credentials configured in .env, authentication is open for LAN
    if not req_user or not req_pass:
        return True

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    is_user_ok = secrets.compare_digest(credentials.username, req_user)
    is_pass_ok = secrets.compare_digest(credentials.password, req_pass)

    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


# --- API Models ---
class SyncRequest(BaseModel):
    gateway: str

class TargetModel(BaseModel):
    id: Optional[str] = None
    name: str
    type: str # "domain", "ip", "api"
    domain: Optional[str] = None
    url: Optional[str] = None
    value: Optional[str] = None
    enabled: bool = True
    prefix: int = 24
    comment: Optional[str] = None

class SettingsModel(BaseModel):
    mikrotik_ip: str
    mikrotik_user: str
    mikrotik_password: Optional[str] = None
    mikrotik_port: int = 8728
    web_username: Optional[str] = None
    web_password: Optional[str] = None


# --- API Routes ---

@app.get("/api/status", dependencies=[Depends(check_auth)])
def get_status():
    mgr = MikroTikManager()
    conn_info = mgr.test_connection()
    routes = []
    if conn_info.get("status") == "ok":
        try:
            routes = mgr.get_active_stream_routes()
        except Exception as e:
            logger.error(f"Error fetching routes: {e}")

    cfg = get_config_env()
    return {
        "connection": conn_info,
        "mikrotik_ip": cfg.get("MIKROTIK_IP", ""),
        "active_stream_routes": routes,
        "routes_count": len(routes),
        "default_gateway": cfg.get("DEFAULT_GATEWAY", "")
    }

@app.get("/api/interfaces", dependencies=[Depends(check_auth)])
def get_interfaces():
    """
    Live query: returns fresh list of interfaces directly from MikroTik with auto-detected gateways.
    """
    mgr = MikroTikManager()
    try:
        ifaces = mgr.get_live_interfaces()
        return {"status": "ok", "interfaces": ifaces}
    except Exception as e:
        logger.error(f"Error reading live interfaces: {e}")
        return {"status": "error", "message": str(e), "interfaces": []}

@app.get("/api/targets", dependencies=[Depends(check_auth)])
def get_targets():
    return {"status": "ok", "targets": load_targets()}

@app.post("/api/targets", dependencies=[Depends(check_auth)])
def add_or_update_target(target: TargetModel):
    targets = load_targets()
    data = target.dict()
    if not data.get("id"):
        data["id"] = f"target_{secrets.token_hex(4)}"
    if not data.get("comment"):
        data["comment"] = f"Stream: {data['name']}"

    # Check if updating existing
    updated = False
    for i, t in enumerate(targets):
        if t.get("id") == data["id"]:
            targets[i] = data
            updated = True
            break
    if not updated:
        targets.append(data)

    save_targets(targets)
    return {"status": "ok", "targets": targets}

@app.delete("/api/targets/{target_id}", dependencies=[Depends(check_auth)])
def delete_target(target_id: str):
    targets = load_targets()
    new_targets = [t for t in targets if t.get("id") != target_id]
    save_targets(new_targets)
    return {"status": "ok", "targets": new_targets}

@app.post("/api/sync", dependencies=[Depends(check_auth)])
def sync_routes(req: SyncRequest):
    if not req.gateway:
        raise HTTPException(status_code=400, detail="Gateway/Interface is required")
    
    mgr = MikroTikManager()
    targets = load_targets()
    try:
        result = mgr.sync_routes(gateway=req.gateway, targets=targets)
        # Remember the last used gateway in config
        save_config_env({"DEFAULT_GATEWAY": req.gateway})
        return result
    except Exception as e:
        logger.error(f"Sync error: {e}")
        return {"status": "error", "message": str(e), "logs": [f"Error: {e}"]}

@app.post("/api/clear", dependencies=[Depends(check_auth)])
def clear_routes():
    mgr = MikroTikManager()
    try:
        result = mgr.clear_stream_routes()
        return result
    except Exception as e:
        logger.error(f"Clear error: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/settings", dependencies=[Depends(check_auth)])
def update_settings(settings: SettingsModel):
    updates = {
        "MIKROTIK_IP": settings.mikrotik_ip,
        "MIKROTIK_USER": settings.mikrotik_user,
        "MIKROTIK_PORT": str(settings.mikrotik_port)
    }
    if settings.mikrotik_password is not None and settings.mikrotik_password != "":
        updates["MIKROTIK_PASSWORD"] = settings.mikrotik_password
    if settings.web_username is not None:
        updates["WEB_USERNAME"] = settings.web_username
    if settings.web_password is not None and settings.web_password != "":
        updates["WEB_PASSWORD"] = settings.web_password

    save_config_env(updates)
    return {"status": "ok", "message": "Settings updated successfully"}


# --- HTML Dashboard ---

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MikroTik Stream Route Manager</title>
    <!-- Tailwind CSS CDN -->
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;500;700;900&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    fontFamily: { sans: ['Vazirmatn', 'system-ui', 'sans-serif'] },
                    colors: {
                        brand: { 500: '#3b82f6', 600: '#2563eb' }
                    }
                }
            }
        }
    </script>
    <style>
        body { font-family: 'Vazirmatn', sans-serif; }
        .glass { background: rgba(30, 41, 59, 0.7); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }
    </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen">

    <!-- Navbar -->
    <header class="border-b border-slate-800 bg-slate-900/60 sticky top-0 z-40 backdrop-blur-md">
        <div class="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
            <div class="flex items-center gap-3">
                <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-blue-600 to-indigo-500 flex items-center justify-center font-bold text-lg shadow-lg shadow-blue-500/20">
                    📡
                </div>
                <div>
                    <h1 class="text-base font-bold tracking-tight">MikroTik Stream Router</h1>
                    <p class="text-xs text-slate-400">Restream & Aparat Policy Routing</p>
                </div>
            </div>

            <!-- Connection Status Badge -->
            <div class="flex items-center gap-3">
                <div id="connBadge" class="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium bg-slate-800 border border-slate-700 text-slate-400">
                    <span id="connDot" class="w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse"></span>
                    <span id="connText">در حال بررسی روتر...</span>
                </div>
                <button onclick="openSettingsModal()" class="p-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 transition" title="تنظیمات">
                    ⚙️
                </button>
            </div>
        </div>
    </header>

    <main class="max-w-6xl mx-auto px-4 py-8 space-y-6">

        <!-- Top Control Bar (Live Interface Selector + Actions) -->
        <section class="glass rounded-2xl p-6 shadow-xl space-y-4">
            <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div>
                    <h2 class="text-lg font-bold flex items-center gap-2">
                        <span>🎯</span> خروجی اینترنت استریم (Interface / Gateway)
                    </h2>
                    <p class="text-xs text-slate-400 mt-1">
                        با انتخاب اینترفیس، گیت‌وی به صورت خودکار شناسایی و روت‌ها بلافاصله به آن منتقل می‌شوند.
                    </p>
                </div>
                
                <div class="flex items-center gap-3">
                    <button onclick="loadInterfaces(true)" class="flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs text-slate-300 font-medium transition" title="استعلام زنده اینترفیس‌ها">
                        <span id="refreshIcon">🔄</span> استعلام زنده اینترفیس‌ها
                    </button>
                </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-12 gap-4 items-center pt-2">
                <!-- Dropdown -->
                <div class="md:col-span-7">
                    <label class="block text-xs text-slate-400 mb-1.5">انتخاب اینترفیس مقصد:</label>
                    <div class="relative">
                        <select id="interfaceSelect" onchange="onInterfaceChange()" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-4 py-3 text-sm text-slate-200 focus:outline-none focus:border-blue-500 font-mono">
                            <option value="">در حال دریافت اینترفیس‌های فعال...</option>
                        </select>
                    </div>
                </div>

                <!-- Custom IP Input Toggle -->
                <div class="md:col-span-5">
                    <label class="block text-xs text-slate-400 mb-1.5">گیت‌وی فعال (شناسایی خودکار / قابل ویرایش):</label>
                    <input type="text" id="customGatewayInput" placeholder="مثلا 192.168.8.1 یا ether3" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-4 py-3 text-sm text-slate-200 focus:outline-none focus:border-blue-500 font-mono text-left" dir="ltr">
                </div>
            </div>

            <!-- Action Buttons -->
            <div class="flex flex-wrap items-center justify-between gap-3 pt-4 border-t border-slate-800">
                <div class="flex items-center gap-2 text-xs text-slate-400">
                    <span>روت‌های فعال فعلی:</span>
                    <span id="activeRoutesCount" class="font-bold text-blue-400">۰</span>
                </div>
                <div class="flex items-center gap-3">
                    <button onclick="clearRoutes()" class="px-4 py-2.5 rounded-xl bg-red-950/40 hover:bg-red-900/60 border border-red-800 text-red-300 text-xs font-semibold transition">
                        🧹 پاک‌سازی روت‌های استریم
                    </button>
                    <button id="syncBtn" onclick="triggerSync()" class="px-6 py-2.5 rounded-xl bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 text-white text-sm font-bold shadow-lg shadow-blue-600/30 transition flex items-center gap-2">
                        <span id="syncSpinner" class="hidden animate-spin">⏳</span>
                        <span>⚡ همگام‌سازی مسیرها (Sync)</span>
                    </button>
                </div>
            </div>
        </section>

        <!-- Targets Section (Aparat, Restream & Custom Domains) -->
        <section class="glass rounded-2xl p-6 shadow-xl space-y-5">
            <div class="flex items-center justify-between">
                <div>
                    <h2 class="text-base font-bold flex items-center gap-2">
                        <span>🌐</span> مقصدهای استریم (Restream, Aparat و دامنه‌های دلخواه)
                    </h2>
                    <p class="text-xs text-slate-400 mt-0.5">
                        آی‌پی این پلتفرم‌ها خودکار تبدیل به رنج شبکه شده و به جدول مسیریابی میکروتیک افزوده می‌شوند.
                    </p>
                </div>
                <button onclick="openAddTargetModal()" class="px-3.5 py-1.5 rounded-xl bg-blue-600/20 hover:bg-blue-600/30 border border-blue-500/40 text-blue-300 text-xs font-medium transition flex items-center gap-1.5">
                    <span>➕</span> افزودن دامنه یا آی‌پی جدید
                </button>
            </div>

            <!-- Targets Grid -->
            <div id="targetsGrid" class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <!-- Dynamically populated -->
            </div>
        </section>

        <!-- Current Active Routes Table -->
        <section class="glass rounded-2xl p-6 shadow-xl space-y-4">
            <div class="flex items-center justify-between">
                <h2 class="text-base font-bold flex items-center gap-2">
                    <span>📋</span> روت‌های استریم ثبت شده در میکروتیک (`/ip/route`)
                </h2>
                <button onclick="loadStatus()" class="text-xs text-slate-400 hover:text-slate-200">
                    🔄 رفرش جدول
                </button>
            </div>

            <div class="overflow-x-auto">
                <table class="w-full text-right text-xs">
                    <thead>
                        <tr class="text-slate-400 border-b border-slate-800">
                            <th class="py-2.5 px-3">مقصد (Dst Address)</th>
                            <th class="py-2.5 px-3">گیت‌وی / اینترفیس</th>
                            <th class="py-2.5 px-3">کامنت</th>
                            <th class="py-2.5 px-3">وضعیت</th>
                        </tr>
                    </thead>
                    <tbody id="routesTableBody" class="divide-y divide-slate-800/60 font-mono">
                        <tr>
                            <td colspan="4" class="py-6 text-center text-slate-500 font-sans">در حال دریافت لیست روت‌ها...</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- Console / Live Logs Box -->
        <section class="glass rounded-2xl p-5 shadow-xl space-y-2">
            <div class="flex items-center justify-between text-xs text-slate-400">
                <span class="font-mono">Console Logs:</span>
                <button onclick="document.getElementById('consoleBox').innerText=''" class="hover:text-slate-200">پاک کردن لاگ</button>
            </div>
            <div id="consoleBox" class="bg-black/60 rounded-xl p-4 font-mono text-xs text-emerald-400 max-h-48 overflow-y-auto space-y-1 text-left" dir="ltr">
                [System] MikroTik Stream Router Web UI Initialized.
            </div>
        </section>

    </main>

    <!-- Modal: Add Custom Target -->
    <div id="addTargetModal" class="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
        <div class="bg-slate-900 border border-slate-700 rounded-2xl p-6 max-w-md w-full space-y-4">
            <h3 class="text-base font-bold">افزودن دامنه یا آی‌پی جدید</h3>
            <p class="text-xs text-slate-400">می‌توانید آدرس دامنه (مثل live.twitch.tv) یا رنج آی‌پی وارد کنید.</p>
            
            <div class="space-y-3">
                <div>
                    <label class="block text-xs text-slate-300 mb-1">نام سرویس:</label>
                    <input type="text" id="targetName" placeholder="مثلا Twitch یا YouTube" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm text-slate-100">
                </div>
                <div>
                    <label class="block text-xs text-slate-300 mb-1">دامنه یا آی‌پی (Domain / IP):</label>
                    <input type="text" id="targetValue" placeholder="live.twitch.tv یا 1.2.3.4" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm text-slate-100 font-mono" dir="ltr">
                </div>
                <div>
                    <label class="block text-xs text-slate-300 mb-1">ساب‌نت روت (CIDR):</label>
                    <select id="targetPrefix" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm text-slate-100">
                        <option value="24">/24 (پیش‌فرض پیشنهادی برای CDN و استریم)</option>
                        <option value="32">/32 (دقیقاً تک آی‌پی بدون رنج)</option>
                    </select>
                </div>
            </div>

            <div class="flex items-center justify-end gap-2 pt-2">
                <button onclick="closeAddTargetModal()" class="px-4 py-2 rounded-xl bg-slate-800 text-xs text-slate-300">لغو</button>
                <button onclick="saveNewTarget()" class="px-4 py-2 rounded-xl bg-blue-600 hover:bg-blue-500 text-xs text-white font-bold">ذخیره</button>
            </div>
        </div>
    </div>

    <!-- Modal: Settings -->
    <div id="settingsModal" class="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
        <div class="bg-slate-900 border border-slate-700 rounded-2xl p-6 max-w-md w-full space-y-4">
            <h3 class="text-base font-bold">تنظیمات اتصال میکروتیک و پنل</h3>
            
            <div class="space-y-3">
                <div>
                    <label class="block text-xs text-slate-300 mb-1">آی‌پی میکروتیک (MikroTik IP):</label>
                    <input type="text" id="cfgMikrotikIp" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm text-slate-100 font-mono" dir="ltr">
                </div>
                <div class="grid grid-cols-2 gap-2">
                    <div>
                        <label class="block text-xs text-slate-300 mb-1">نام کاربری API:</label>
                        <input type="text" id="cfgMikrotikUser" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm text-slate-100 font-mono" dir="ltr">
                    </div>
                    <div>
                        <label class="block text-xs text-slate-300 mb-1">پورت API:</label>
                        <input type="number" id="cfgMikrotikPort" value="8728" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm text-slate-100 font-mono" dir="ltr">
                    </div>
                </div>
                <div>
                    <label class="block text-xs text-slate-300 mb-1">رمز عبور میکروتیک (اختیاری اگر تغییر نکرده):</label>
                    <input type="password" id="cfgMikrotikPass" placeholder="••••••••" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm text-slate-100 font-mono" dir="ltr">
                </div>
            </div>

            <div class="flex items-center justify-end gap-2 pt-2">
                <button onclick="closeSettingsModal()" class="px-4 py-2 rounded-xl bg-slate-800 text-xs text-slate-300">لغو</button>
                <button onclick="saveSettings()" class="px-4 py-2 rounded-xl bg-blue-600 hover:bg-blue-500 text-xs text-white font-bold">ذخیره تنظیمات</button>
            </div>
        </div>
    </div>

    <!-- JavaScript Application Logic -->
    <script>
        function log(msg) {
            const box = document.getElementById('consoleBox');
            const time = new Date().toLocaleTimeString();
            box.innerText += `\\n[${time}] ${msg}`;
            box.scrollTop = box.scrollHeight;
        }

        async function loadStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                const badge = document.getElementById('connBadge');
                const dot = document.getElementById('connDot');
                const text = document.getElementById('connText');

                if (data.connection.status === 'ok') {
                    dot.className = "w-2.5 h-2.5 rounded-full bg-emerald-400";
                    text.innerText = `روتر متصل: ${data.connection.identity} (${data.mikrotik_ip})`;
                    badge.className = "flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium bg-emerald-950/40 border border-emerald-800 text-emerald-300";
                } else {
                    dot.className = "w-2.5 h-2.5 rounded-full bg-red-400";
                    text.innerText = `خطای اتصال به میکروتیک: ${data.connection.message}`;
                    badge.className = "flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium bg-red-950/40 border border-red-800 text-red-300";
                }

                document.getElementById('activeRoutesCount').innerText = data.routes_count;

                // Render active routes table
                const tbody = document.getElementById('routesTableBody');
                if (!data.active_stream_routes || data.active_stream_routes.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="4" class="py-6 text-center text-slate-500 font-sans">هیچ روت استریمی هنوز ثبت نشده است. از بالا یک اینترفیس انتخاب کنید.</td></tr>`;
                } else {
                    tbody.innerHTML = data.active_stream_routes.map(r => `
                        <tr class="hover:bg-slate-900/40 transition">
                            <td class="py-2.5 px-3 font-bold text-slate-200">${r.dst_address}</td>
                            <td class="py-2.5 px-3 text-blue-400">${r.gateway}</td>
                            <td class="py-2.5 px-3 text-slate-300 font-sans">${r.comment}</td>
                            <td class="py-2.5 px-3">
                                <span class="px-2 py-0.5 rounded-md text-[10px] ${r.disabled === 'false' ? 'bg-emerald-950 text-emerald-400 border border-emerald-800' : 'bg-red-950 text-red-400'}">
                                    ${r.disabled === 'false' ? 'Active' : 'Disabled'}
                                </span>
                            </td>
                        </tr>
                    `).join('');
                }

                if (data.default_gateway && !document.getElementById('customGatewayInput').value) {
                    document.getElementById('customGatewayInput').value = data.default_gateway;
                }

            } catch (err) {
                log('Error loading status: ' + err);
            }
        }

        async function loadInterfaces(showAnim = false) {
            const icon = document.getElementById('refreshIcon');
            if (showAnim) icon.classList.add('animate-spin');

            try {
                log("Fetching LIVE interfaces from MikroTik...");
                const res = await fetch('/api/interfaces');
                const data = await res.json();
                const sel = document.getElementById('interfaceSelect');
                
                if (data.status === 'ok' && data.interfaces.length > 0) {
                    sel.innerHTML = '<option value="">-- انتخاب اینترفیس خروجی --</option>' + 
                        data.interfaces.map(iface => {
                            const iconStatus = iface.running ? '🟢' : '⚪';
                            const gw = iface.gateway_ip ? `GW: ${iface.gateway_ip}` : '';
                            const isp = iface.isp ? `[${iface.isp.toUpperCase()}]` : '';
                            const ips = iface.ips.length ? `(IP: ${iface.ips[0]})` : '';
                            const extra = [isp, gw, ips].filter(Boolean).join(' ');
                            return `<option value="${iface.name}" data-gateway="${iface.gateway_ip || ''}" data-isp="${iface.isp || ''}">${iconStatus} ${iface.name} ${extra}</option>`;
                        }).join('');
                    log(`Received ${data.interfaces.length} live interfaces.`);
                } else {
                    sel.innerHTML = `<option value="">خطا در دریافت اینترفیس‌ها (${data.message || 'ناموفق'})</option>`;
                }
            } catch (err) {
                log('Error loading interfaces: ' + err);
            } finally {
                if (showAnim) setTimeout(() => icon.classList.remove('animate-spin'), 600);
            }
        }

        function onInterfaceChange() {
            const sel = document.getElementById('interfaceSelect');
            const selectedOption = sel.options[sel.selectedIndex];
            if (!selectedOption || !sel.value) return;

            const gwIp = selectedOption.getAttribute('data-gateway') || '';
            const isp = selectedOption.getAttribute('data-isp') || '';
            const ifaceName = sel.value;

            // Automatically choose Gateway IP if known, otherwise interface name
            const effectiveGateway = gwIp || ifaceName;
            document.getElementById('customGatewayInput').value = effectiveGateway;

            const ispDesc = isp ? ` (${isp.toUpperCase()})` : '';
            log(`🎯 اینترفیس ${ifaceName}${ispDesc} انتخاب شد -> گیت‌وی روی ${effectiveGateway} تنظیم شد. برای اعمال، دکمه همگام‌سازی را بزنید.`);
        }

        async function loadTargets() {
            try {
                const res = await fetch('/api/targets');
                const data = await res.json();
                const container = document.getElementById('targetsGrid');

                container.innerHTML = data.targets.map(t => {
                    const isDomain = t.type === 'domain';
                    const isApi = t.type === 'api';
                    const desc = isDomain ? `دامنه: ${t.domain}` : (isApi ? `API منبع آی‌پی‌ها: ${t.url}` : `مقدار: ${t.value || t.domain}`);
                    
                    return `
                        <div class="p-4 rounded-xl bg-slate-900/80 border border-slate-800 flex items-center justify-between gap-3">
                            <div>
                                <div class="flex items-center gap-2">
                                    <span class="text-base">${t.id === 'aparat' ? '🇮🇷' : (t.id === 'restream' ? '🚀' : '🔗')}</span>
                                    <h4 class="text-sm font-bold text-slate-100">${t.name}</h4>
                                    <span class="px-2 py-0.5 rounded text-[10px] bg-slate-800 text-slate-400 font-mono">/${t.prefix || 24}</span>
                                </div>
                                <p class="text-xs text-slate-400 mt-1 font-mono truncate max-w-xs" dir="ltr">${desc}</p>
                            </div>
                            <div class="flex items-center gap-2">
                                <label class="relative inline-flex items-center cursor-pointer">
                                    <input type="checkbox" ${t.enabled ? 'checked' : ''} onchange="toggleTarget('${t.id}', this.checked)" class="sr-only peer">
                                    <div class="w-9 h-5 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-blue-600"></div>
                                </label>
                                ${t.id !== 'aparat' && t.id !== 'restream' ? `
                                    <button onclick="deleteTarget('${t.id}')" class="text-slate-500 hover:text-red-400 text-sm p-1">🗑️</button>
                                ` : ''}
                            </div>
                        </div>
                    `;
                }).join('');
            } catch (err) {
                log('Error loading targets: ' + err);
            }
        }

        async function toggleTarget(id, enabled) {
            try {
                const res = await fetch('/api/targets');
                const data = await res.json();
                const target = data.targets.find(t => t.id === id);
                if (target) {
                    target.enabled = enabled;
                    await fetch('/api/targets', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(target)
                    });
                    log(`Target ${target.name} ${enabled ? 'enabled' : 'disabled'}.`);
                    loadTargets();
                }
            } catch (err) {
                log('Error toggling target: ' + err);
            }
        }

        async function deleteTarget(id) {
            if (!confirm('آیا از حذف این مقصد اطمینان دارید؟')) return;
            try {
                await fetch(`/api/targets/${id}`, { method: 'DELETE' });
                log(`Target ${id} deleted.`);
                loadTargets();
            } catch (err) {
                log('Error deleting target: ' + err);
            }
        }

        async function triggerSync(explicitGateway = null) {
            const ifaceVal = document.getElementById('interfaceSelect').value;
            const customVal = document.getElementById('customGatewayInput').value.trim();
            const gateway = explicitGateway || customVal || ifaceVal;

            if (!gateway) {
                alert('لطفا ابتدا یک اینترفیس یا یک آی‌پی گیت‌وی را انتخاب کنید.');
                return;
            }

            const btn = document.getElementById('syncBtn');
            const spinner = document.getElementById('syncSpinner');
            btn.disabled = true;
            spinner.classList.remove('hidden');

            log(`Starting SYNC with Gateway/Interface: ${gateway}...`);

            try {
                const res = await fetch('/api/sync', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ gateway: gateway })
                });
                const data = await res.json();

                if (data.status === 'ok') {
                    log(`✅ تغییرات اعمال شد! افزوده شده: ${data.added} | بروزرسانی شده: ${data.updated} | بدون تغییر: ${data.unchanged}`);
                    if (data.logs) {
                        data.logs.forEach(l => log(l));
                    }
                    loadStatus();
                } else {
                    log(`❌ خطا در همگام‌سازی: ${data.message}`);
                    if (data.logs) {
                        data.logs.forEach(l => log(l));
                    }
                }
            } catch (err) {
                log('Sync request failed: ' + err);
            } finally {
                btn.disabled = false;
                spinner.classList.add('hidden');
            }
        }

        async function clearRoutes() {
            if (!confirm('آیا مطمئن هستید که می‌خواهید تمام روت‌های ایجاد شده توسط این اسکریپت را از میکروتیک پاک کنید؟')) return;
            log('Clearing all stream routes from MikroTik...');
            try {
                const res = await fetch('/api/clear', { method: 'POST' });
                const data = await res.json();
                if (data.status === 'ok') {
                    log(`Successfully removed ${data.removed} routes.`);
                    loadStatus();
                } else {
                    log(`Clear failed: ${data.message}`);
                }
            } catch (err) {
                log('Clear error: ' + err);
            }
        }

        // Modals
        function openAddTargetModal() {
            document.getElementById('addTargetModal').classList.remove('hidden');
        }
        function closeAddTargetModal() {
            document.getElementById('addTargetModal').classList.add('hidden');
        }
        async function saveNewTarget() {
            const name = document.getElementById('targetName').value.trim();
            const val = document.getElementById('targetValue').value.trim();
            const prefix = parseInt(document.getElementById('targetPrefix').value);

            if (!name || !val) {
                alert('لطفاً نام و آدرس دامنه/آی‌پی را وارد کنید.');
                return;
            }

            await fetch('/api/targets', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: name,
                    type: 'custom',
                    value: val,
                    prefix: prefix,
                    enabled: true
                })
            });

            closeAddTargetModal();
            document.getElementById('targetName').value = '';
            document.getElementById('targetValue').value = '';
            loadTargets();
            log(`Added target ${name} (${val}).`);
        }

        function openSettingsModal() {
            document.getElementById('settingsModal').classList.remove('hidden');
        }
        function closeSettingsModal() {
            document.getElementById('settingsModal').classList.add('hidden');
        }
        async function saveSettings() {
            const ip = document.getElementById('cfgMikrotikIp').value.trim();
            const user = document.getElementById('cfgMikrotikUser').value.trim();
            const port = parseInt(document.getElementById('cfgMikrotikPort').value) || 8728;
            const pass = document.getElementById('cfgMikrotikPass').value;

            await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    mikrotik_ip: ip,
                    mikrotik_user: user,
                    mikrotik_password: pass || null,
                    mikrotik_port: port
                })
            });

            closeSettingsModal();
            log('Settings updated. Reconnecting...');
            loadStatus();
            loadInterfaces(true);
        }

        // Initial Load
        window.addEventListener('DOMContentLoaded', () => {
            loadStatus();
            loadInterfaces(true);
            loadTargets();
        });
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
def index(request: Request):
    return HTMLResponse(content=HTML_TEMPLATE, status_code=200)


def start_server():
    cfg = get_config_env()
    port = int(cfg.get("WEB_PORT", 18542))
    host = "0.0.0.0"
    logger.info(f"Starting MikroTik Stream Router Web Panel on http://{host}:{port}")
    uvicorn.run("app:app", host=host, port=port, reload=False)

if __name__ == "__main__":
    start_server()
