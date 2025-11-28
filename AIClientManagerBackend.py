import threading
import logging
import json
from enum import Enum
from typing import Optional, Callable, Any
from flask import Flask, Blueprint, jsonify, request, Response, abort
from werkzeug.exceptions import HTTPException


try:
    from .LimitMixins import ClientMetricsMixin
    from .AIClientManager import AIClientManager, ClientStatus, BaseAIClient, CLIENT_PRIORITY_NORMAL
except ImportError:
    from LimitMixins import ClientMetricsMixin
    from AIClientManager import AIClientManager, ClientStatus, BaseAIClient, CLIENT_PRIORITY_NORMAL


logger = logging.getLogger("AIDashboard")

FRONTEND_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Client Manager</title>

    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">

    <style>
        .progress-bar { transition: width 0.5s ease; }
        [v-cloak] { display: none; }
        /* 饱和时的条纹背景效果 */
        .bg-striped {
            background-image: linear-gradient(45deg,rgba(255,255,255,.15) 25%,transparent 25%,transparent 50%,rgba(255,255,255,.15) 50%,rgba(255,255,255,.15) 75%,transparent 75%,transparent);
            background-size: 1rem 1rem;
        }
    </style>
</head>
<body class="bg-gray-100 text-gray-800 font-sans">
<div id="app" v-cloak class="min-h-screen p-6">
    <div class="max-w-7xl mx-auto mb-8 flex justify-between items-center">
        <div>
            <h1 class="text-3xl font-bold text-gray-900">
                <i class="fa-solid fa-server mr-2 text-indigo-600"></i> AI Client Manager
            </h1>
            <p class="text-sm text-gray-500 mt-1">Last Updated: {{ lastUpdated }}</p>
        </div>
        <div class="flex space-x-4">
             <button @click="fetchData" class="px-4 py-2 bg-white border rounded shadow hover:bg-gray-50 text-sm">
                <i class="fa-solid fa-rotate-right" :class="{'fa-spin': loading}"></i> Refresh
            </button>
        </div>
    </div>

    <div class="max-w-7xl mx-auto grid grid-cols-1 md:grid-cols-4 gap-6 mb-8" v-if="stats.summary">
        <div class="bg-white rounded-lg shadow p-6 border-l-4 border-indigo-500">
            <div class="text-gray-500 text-sm uppercase font-semibold">Total Clients</div>
            <div class="text-3xl font-bold mt-2">{{ stats.summary.total_clients }}</div>
        </div>
        <div class="bg-white rounded-lg shadow p-6 border-l-4 border-green-500">
            <div class="text-gray-500 text-sm uppercase font-semibold">Available</div>
            <div class="text-3xl font-bold mt-2 text-green-600">{{ stats.summary.available }}</div>
        </div>
        <div class="bg-white rounded-lg shadow p-6 border-l-4 border-yellow-500">
            <div class="text-gray-500 text-sm uppercase font-semibold">Busy / Active</div>
            <div class="text-3xl font-bold mt-2 text-yellow-600">{{ stats.summary.busy }}</div>
            <div class="text-xs text-gray-400 mt-1">Load: {{ stats.summary.system_load }}</div>
        </div>
        <div class="bg-white rounded-lg shadow p-6 border-l-4 border-red-500">
            <div class="text-gray-500 text-sm uppercase font-semibold">Errors / Unavail</div>
            <div class="text-3xl font-bold mt-2 text-red-600">{{ stats.summary.clients_with_errors }}</div>
        </div>
    </div>

    <div class="max-w-7xl mx-auto bg-white shadow rounded-lg overflow-hidden">
        <div class="px-6 py-4 border-b border-gray-200 flex justify-between items-center bg-gray-50">
            <h2 class="text-lg font-semibold text-gray-700">Client Groups & Instances</h2>
            <span class="text-xs px-2 py-1 bg-gray-200 rounded text-gray-600">Auto-refresh: 2s</span>
        </div>

        <div class="overflow-x-auto">
            <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                    <tr>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Client Name / Model</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Health & Metrics</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Allocation</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Runtime Stats</th>
                        <th class="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">Actions</th>
                    </tr>
                </thead>

                <tbody class="bg-white" v-for="(group, groupName) in groupedClients" :key="groupName">

                    <tr class="bg-gray-100 border-t border-b border-gray-200">
                        <td colspan="6" class="px-6 py-2">
                            <div class="flex items-center justify-between">
                                <div class="flex items-center space-x-4">
                                    <span class="font-bold text-gray-700 text-sm">
                                        <i class="fa-solid fa-layer-group text-gray-400 mr-2"></i>{{ groupName }}
                                    </span>
                                    <div class="flex items-center text-xs bg-white px-2 py-1 rounded border border-gray-300 shadow-sm"
                                         :class="{'border-orange-300 bg-orange-50 text-orange-700': group.isSaturated}">
                                        <span class="mr-2 font-semibold">Concurrency:</span>
                                        <span class="font-mono" :class="{'text-red-600 font-bold': group.isSaturated}">
                                            {{ group.activeCount }}
                                        </span>
                                        <span class="mx-1 text-gray-400">/</span>
                                        <span class="font-mono">{{ group.limit === Infinity ? '∞' : group.limit }}</span>

                                        <span v-if="group.isSaturated" class="ml-2 text-[10px] font-bold uppercase bg-orange-200 text-orange-800 px-1 rounded animate-pulse">
                                            Limit Reached
                                        </span>
                                    </div>
                                </div>
                                <div v-if="group.limit !== Infinity" class="w-32 h-1.5 bg-gray-300 rounded-full overflow-hidden">
                                    <div class="h-full bg-indigo-500 transition-all duration-500"
                                         :class="{'bg-red-500': group.isSaturated, 'bg-green-500': group.activeCount < group.limit}"
                                         :style="{ width: Math.min((group.activeCount / group.limit) * 100, 100) + '%' }">
                                    </div>
                                </div>
                            </div>
                        </td>
                    </tr>

                    <tr v-for="client in group.clients" :key="client.meta.name" 
                        class="hover:bg-gray-50 transition border-b border-gray-100 last:border-0"
                        :class="{'opacity-60 bg-gray-50': isBlockedByGroupLimit(client, group)}">

                        <td class="px-6 py-4 whitespace-nowrap">
                            <div class="flex items-center">
                                <div>
                                    <div class="text-sm font-bold text-gray-900 flex items-center">
                                        {{ client.meta.name }}
                                        <i v-if="isBlockedByGroupLimit(client, group)" 
                                           class="fa-solid fa-ban text-red-400 ml-2" 
                                           title="Blocked by group concurrency limit"></i>
                                    </div>
                                    <div class="text-xs text-gray-500">Type: {{ client.meta.type }}</div>
                                    <div class="text-xs text-indigo-600 font-mono mt-1" v-if="client.meta.current_model && client.meta.current_model !== 'Unknown'">
                                        <i class="fa-solid fa-microchip mr-1"></i>{{ client.meta.current_model }}
                                    </div>
                                    <div class="text-xs text-gray-500 mt-1">Priority: <span class="font-mono bg-gray-100 px-1 rounded">{{ client.meta.priority }}</span></div>
                                </div>
                            </div>
                        </td>

                        <td class="px-6 py-4 whitespace-nowrap">
                            <div class="flex flex-col items-start">
                                <span :class="getStatusBadgeClass(client)" class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full items-center">
                                    <span class="w-2 h-2 rounded-full mr-2" :class="getStatusDotClass(client)"></span>
                                    {{ formatStatus(client.state.status) }}
                                </span>

                                <div v-if="client.state.is_busy" class="text-xs text-yellow-600 mt-1 font-semibold animate-pulse">● IN USE</div>

                                <div v-if="isBlockedByGroupLimit(client, group)" class="text-[10px] text-red-500 mt-1 font-bold border border-red-200 bg-red-50 px-1 rounded">
                                    BLOCKED BY LIMIT
                                </div>
                            </div>
                        </td>

                        <td class="px-6 py-4 align-top w-64">
                            <div class="mb-2">
                                <div class="flex justify-between text-xs mb-1">
                                    <span>Health</span>
                                    <span class="font-bold">{{ client.state.health_score }}%</span>
                                </div>
                                <div class="w-full bg-gray-200 rounded-full h-2">
                                    <div class="h-2 rounded-full progress-bar" 
                                         :class="getHealthColor(client.state.health_score)"
                                         :style="{ width: client.state.health_score + '%' }"></div>
                                </div>
                            </div>
                            <div v-if="client.metrics && client.metrics.length > 0" class="space-y-1">
                                <div v-for="m in client.metrics.slice(0, 2)" class="text-xs text-gray-500 flex justify-between">
                                    <span>{{ formatMetricKey(m.key) }}:</span>
                                    <span>{{ formatNumber(m.current) }} / {{ formatNumber(m.target) }}</span>
                                </div>
                            </div>
                        </td>

                        <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                             <div v-if="isSystemCheck(client.allocation.held_by)" class="bg-purple-50 border border-purple-100 rounded p-2">
                                <div class="text-purple-700 font-bold flex items-center">
                                    <i class="fa-solid fa-stethoscope mr-2 animate-pulse"></i> 
                                    <span>Self Check</span>
                                </div>
                                <div class="text-xs mt-1 text-purple-600">
                                    Duration: {{ formatDuration(client.allocation.duration_seconds) }}
                                </div>
                            </div>
                            <div v-else-if="client.allocation.held_by" class="bg-indigo-50 border border-indigo-100 rounded p-2">
                                <div class="text-indigo-700 font-bold overflow-hidden text-ellipsis">
                                    <i class="fa-regular fa-user mr-1"></i> 
                                    {{ client.allocation.held_by }}
                                </div>
                                <div class="text-xs mt-1">
                                    Duration: {{ formatDuration(client.allocation.duration_seconds) }}
                                </div>
                            </div>
                            <div v-else class="text-gray-400 text-xs">Idle</div>
                            <div class="text-xs text-gray-400 mt-1">
                                Last Active: {{ timeAgo(client.state.last_active_ts) }}
                            </div>
                        </td>

                        <td class="px-6 py-4 whitespace-nowrap text-xs">
                             <div class="flex flex-col space-y-1">
                                <span class="text-gray-600">Calls: <b>{{ client.runtime_stats.chat_count }}</b></span>
                                <span class="text-gray-600">
                                    Heat: 
                                    <b :class="getHeatClass(client.runtime_stats.error_count)">
                                        {{ client.runtime_stats.error_count || 0 }}
                                        <i v-if="client.runtime_stats.error_count > 0" class="fa-solid fa-fire text-[10px] ml-1"></i>
                                    </b>
                                </span>
                                <span class="text-gray-600">Errors: <b :class="{'text-red-600': client.runtime_stats.error_sum > 0}">{{ client.runtime_stats.error_sum }}</b></span>
                                <span class="text-gray-400">Rate: {{ client.runtime_stats.error_rate_percent }}%</span>
                            </div>
                        </td>

                        <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                            <div class="flex flex-col space-y-2 items-end">
                                <button @click="triggerCheck(client.meta.name)" class="text-indigo-600 hover:text-indigo-900 text-xs bg-indigo-50 px-2 py-1 rounded border border-indigo-200 hover:bg-indigo-100 transition">
                                    <i class="fa-solid fa-stethoscope mr-1"></i> Check Health
                                </button>
                                <div class="relative group">
                                    <button class="text-gray-500 hover:text-gray-700 text-xs px-2 py-1">Change Status <i class="fa-solid fa-caret-down"></i></button>
                                    <div class="absolute right-0 mt-1 w-32 bg-white border border-gray-200 shadow-lg rounded hidden group-hover:block z-10">
                                        <a href="#" @click.prevent="setStatus(client.meta.name, 'available')" class="block px-4 py-2 text-xs text-gray-700 hover:bg-green-50 hover:text-green-700">Set Available</a>
                                        <a href="#" @click.prevent="setStatus(client.meta.name, 'error')" class="block px-4 py-2 text-xs text-gray-700 hover:bg-red-50 hover:text-red-700">Set Error</a>
                                        <a href="#" @click.prevent="setStatus(client.meta.name, 'unavailable')" class="block px-4 py-2 text-xs text-gray-700 hover:bg-gray-100">Set Unavailable</a>
                                    </div>
                                </div>
                            </div>
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>
</div>
<script>
    const { createApp } = Vue;
    createApp({
        data() { return { stats: { summary: null, clients: [] }, loading: false, lastUpdated: '-', timer: null } },
        mounted() { this.fetchData(); this.timer = setInterval(this.fetchData, 2000); },
        computed: {
            // 核心逻辑：将 flat list 转换为按 group 聚合的对象
            groupedClients() {
                if (!this.stats.clients) return {};

                const groups = {};
                const limits = (this.stats.summary && this.stats.summary.group_limits) ? this.stats.summary.group_limits : {};

                // 1. 分组 & 基础统计
                this.stats.clients.forEach(c => {
                    // 确保 group_id 存在，你需要在 python 的 meta 中加入 group_id
                    const gid = c.meta.group_id || 'Default Group';

                    if (!groups[gid]) {
                        groups[gid] = {
                            clients: [],
                            limit: limits[gid] !== undefined ? limits[gid] : Infinity,
                            activeCount: 0,
                            isSaturated: false
                        };
                    }
                    groups[gid].clients.push(c);

                    // 计算当前组内忙碌的 client (根据你的 Manager 逻辑，busy = 占用名额)
                    if (c.state.is_busy) {
                        groups[gid].activeCount++;
                    }
                });

                // 2. 计算饱和状态
                for (const gid in groups) {
                    const g = groups[gid];
                    if (g.limit !== Infinity && g.activeCount >= g.limit) {
                        g.isSaturated = true;
                    }
                    // 按优先级对组内 client 排序 (数字越小优先级越高)
                    g.clients.sort((a, b) => a.meta.priority - b.meta.priority);
                }

                // 3. (可选) 对 Group 本身排序? 比如按字母或者按饱和度
                // 这里返回对象，Vue 的 v-for 遍历对象顺序可能不固定，但现代浏览器一般按插入序
                // 如果需要严格顺序，可以返回 Array。这里暂且返回 Object。
                return groups;
            }
        },
        methods: {
            async fetchData() {
                try {
                    const res = await fetch('api/overview');
                    if (!res.ok) throw new Error(res.statusText);
                    const data = await res.json();
                    this.stats = data;
                    this.lastUpdated = new Date().toLocaleTimeString();
                } catch (e) { console.error("Fetch error", e); }
            },

            // 判断一个 client 是否因为组限制而实际上不可用
            // 条件：Client 是 Available 且 Idle，但 Group 已经 Saturated
            isBlockedByGroupLimit(client, group) {
                if (!client || !group) return false;
                const isAvailable = String(client.state.status).toUpperCase().includes('AVAILABLE');
                const isIdle = !client.state.is_busy;

                return isAvailable && isIdle && group.isSaturated;
            },

            // --- 其他原有 Helper 方法保持不变 ---
            async triggerCheck(name) {
                if(!confirm(`Force health check for ${name}?`)) return;
                try { await fetch(`api/clients/${name}/check`, { method: 'POST' }); setTimeout(this.fetchData, 500); } catch (e) { alert("Action failed"); }
            },
            async setStatus(name, status) {
                if(!confirm(`Set ${name} to ${status}?`)) return;
                try {
                    await fetch(`api/clients/${name}/status`, { 
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({status: status})
                    });
                    setTimeout(this.fetchData, 500);
                } catch (e) { alert("Action failed"); }
            },
            isSystemCheck(name) { return name && String(name).includes('[System Check]'); },
            getHeatClass(heat) {
                if (!heat || heat === 0) return 'text-gray-400 font-normal'; 
                if (heat < 3) return 'text-orange-500 font-bold';            
                return 'text-red-600 font-bold animate-pulse';               
            },
            getStatusBadgeClass(c) { 
                const s = String(c.state.status).toUpperCase(); 
                if (s.includes('AVAILABLE')) return 'bg-green-50 text-green-700 ring-1 ring-inset ring-green-600/20'; 
                if (s.includes('ERROR') || s.includes('FAIL')) return 'bg-red-50 text-red-700 ring-1 ring-inset ring-red-600/10'; 
                if (s.includes('BUSY')) return 'bg-yellow-50 text-yellow-800 ring-1 ring-inset ring-yellow-600/20';
                if (s.includes('UNAVAILABLE') || s.includes('OFFLINE')) return 'bg-gray-50 text-gray-600 ring-1 ring-inset ring-gray-500/10'; 
                return 'bg-blue-50 text-blue-700 ring-1 ring-inset ring-blue-700/10'; 
            },
            getStatusDotClass(c) { 
                const s = String(c.state.status).toUpperCase(); 
                if (s.includes('AVAILABLE')) return 'bg-green-500';
                if (s.includes('ERROR') || s.includes('FAIL')) return 'bg-red-500';
                if (s.includes('BUSY')) return 'bg-yellow-500';
                if (s.includes('UNAVAILABLE') || s.includes('OFFLINE')) return 'bg-gray-400';
                return 'bg-blue-500'; 
            },
            formatStatus(s) { return s.split('.').pop(); },
            getHealthColor(s) { if (s > 80) return 'bg-green-500'; if (s > 50) return 'bg-yellow-500'; return 'bg-red-500'; },
            formatMetricKey(k) { return k.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()); },
            formatNumber(n) { if (n >= 1000) return (n/1000).toFixed(1) + 'k'; return n; },
            formatDuration(s) { if (s < 60) return parseInt(s) + 's'; return parseInt(s / 60) + 'm'; },
            timeAgo(ts) { if (!ts) return '-'; const d = (Date.now()/1000) - ts; if (d < 60) return parseInt(d) + 's ago'; if (d < 3600) return parseInt(d/60) + 'm ago'; return parseInt(d/3600) + 'h ago'; }
        }
    }).mount('#app');
</script>
</body>
</html>
"""


class AIDashboardService:
    """
    Flask-compatible dashboard service for AI Client Manager.
    """

    def __init__(self, manager: AIClientManager):
        self.manager = manager
        self._is_registered = False

    def _make_json_serializable(self, obj: Any) -> Any:
        """
        Recursively convert objects (like Enums) to JSON-serializable formats.
        """
        if isinstance(obj, dict):
            return {k: self._make_json_serializable(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._make_json_serializable(v) for v in obj]
        elif isinstance(obj, Enum):
            return obj.value
        return obj

    def create_blueprint(self, wrapper: Optional[Callable] = None) -> Blueprint:
        """
        Create a Flask Blueprint containing the dashboard routes.

        Args:
            wrapper: Optional decorator function to wrap endpoints (e.g., for auth).
                     Example: login_required
        """
        bp = Blueprint('ai_dashboard', __name__)

        def maybe_wrap(endpoint_func):
            if wrapper:
                return wrapper(endpoint_func)
            return endpoint_func

        # --- Endpoints ---

        @bp.route('/', methods=['GET'])
        @maybe_wrap
        def dashboard_view():
            """Serve the Vue.js Frontend"""
            return Response(FRONTEND_HTML, mimetype='text/html')

        @bp.route('/api/overview', methods=['GET'])
        @maybe_wrap
        def get_overview():
            """Get system stats and inject current model info"""
            raw_stats = self.manager.get_client_stats()

            # 使用 ai_core.py 中 Manager 的 get_client_by_name 和 BaseAIClient 的 get_current_model
            for client_data in raw_stats.get('clients', []):
                c_name = client_data.get('meta', {}).get('name')

                # 直接使用 Manager 的辅助方法查找对象
                real_client = self.manager.get_client_by_name(c_name)

                # 使用统一接口获取模型
                model_info = "Unknown"
                if real_client:
                    # 这个接口已经在 ai_core.py 的 BaseAIClient 中定义
                    model_info = real_client.get_current_model()

                client_data['meta']['current_model'] = model_info

            serializable_stats = self._make_json_serializable(raw_stats)
            return jsonify(serializable_stats)

        @bp.route('/api/clients/<client_name>/check', methods=['POST'])
        @maybe_wrap
        def trigger_health_check(client_name):
            """Manually trigger client health check"""

            # 使用 ai_core.py 中 Manager 新增的 trigger_manual_check 接口

            # 在后台线程中运行，因为该操作包含锁和网络IO
            def run_check():
                self.manager.trigger_manual_check(client_name)

            threading.Thread(target=run_check, daemon=True).start()
            return jsonify({"message": f"Health check triggered for {client_name}"})

        @bp.route('/api/clients/<client_name>/status', methods=['POST'])
        @maybe_wrap
        def update_client_status(client_name):
            """Manually update client status"""
            data = request.get_json()
            if not data or 'status' not in data:
                return jsonify({"error": "Missing status field"}), 400

            try:
                new_status_str = data['status'].upper()
                new_status = ClientStatus[new_status_str]

                # 使用 ai_core.py 中 Manager 新增的 set_client_status 接口
                success = self.manager.set_client_status(client_name, new_status)

                if success:
                    return jsonify({"message": f"Status updated to {new_status}"})
                else:
                    return jsonify({"error": "Client not found"}), 404

            except KeyError:
                return jsonify({"error": "Invalid status code"}), 400

        return bp

    def mount_to_app(self, app: Flask, url_prefix: str = "/ai-dashboard", wrapper: Optional[Callable] = None) -> bool:
        """
        Mount the dashboard to a Flask app instance.
        """
        if self._is_registered:
            logger.warning("Dashboard blueprint already registered.")
            return False

        bp = self.create_blueprint(wrapper)
        app.register_blueprint(bp, url_prefix=url_prefix)
        self._is_registered = True
        logger.info(f"AI Dashboard mounted at {url_prefix}")
        return True

    def run_standalone(self, host="0.0.0.0", port=8000, debug=False):
        """Run as a standalone Flask app."""
        app = Flask(__name__)
        self.mount_to_app(app, url_prefix="")
        print(f"Starting standalone dashboard at http://{host}:{port}")
        app.run(host=host, port=port, debug=debug)
