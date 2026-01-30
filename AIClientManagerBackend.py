import time
import logging
import threading
from enum import Enum
from typing import Optional, Callable, Any
from flask import Flask, Blueprint, jsonify, request, Response


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
          <button onclick="window.location.href='timeline'" class="px-4 py-2 bg-white border rounded shadow hover:bg-gray-50 text-sm">
            <i class="fa-solid fa-chart-gantt mr-1"></i> Timeline
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


FRONTEND_TIMELINE_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>AI Client Timeline</title>

  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
  <script src="https://cdn.plot.ly/plotly-2.30.0.min.js"></script>
  <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">

  <style>
    [v-cloak]{display:none;}
    body { background: #f6f7fb; }
    .card { background:#fff; border:1px solid rgba(15,23,42,.08); border-radius:14px; }
    .muted { color: rgba(15,23,42,.60); }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono","Courier New", monospace; }
    .btn { border:1px solid rgba(15,23,42,.10); background:#fff; }
    .btn:hover { background: rgba(15,23,42,.03); }
    .btn-primary { background:#4f46e5; color:#fff; border:1px solid rgba(79,70,229,.55); }
    .btn-primary:hover { background:#4338ca; }
    .pill { border:1px solid rgba(15,23,42,.10); background:#fff; }
    .pill:hover { background: rgba(15,23,42,.03); }
    .pill-active { border-color: rgba(79,70,229,.45); box-shadow: 0 0 0 2px rgba(79,70,229,.10); color:#4338ca; }
    #timelinePlot { min-height: 520px; }
    input[type="range"] { accent-color: #4f46e5; }
  </style>
</head>

<body class="text-gray-900">
<div id="app" v-cloak class="min-h-screen p-4 md:p-6">

  <!-- Header -->
  <div class="max-w-7xl mx-auto flex flex-col md:flex-row md:items-center md:justify-between gap-3 mb-4">
    <div class="flex items-center gap-3">
      <div class="w-10 h-10 rounded-xl bg-indigo-600 text-white flex items-center justify-center shadow-sm">
        <i class="fa-solid fa-chart-gantt"></i>
      </div>
      <div>
        <div class="text-xl font-bold">Client Timeline</div>
        <div class="text-xs muted mt-0.5 flex flex-wrap items-center gap-2">
          <span>Session: <span class="mono text-gray-700">{{ sessionId || '-' }}</span></span>
          <span class="text-gray-300">•</span>
          <span>Updated: {{ lastUpdated }}</span>
          <span v-if="loading" class="text-indigo-600">
            <i class="fa-solid fa-circle-notch fa-spin mr-1"></i>Loading
          </span>
        </div>
      </div>
    </div>

    <div class="flex items-center gap-2 justify-end">
      <a href="./" class="btn px-3 py-2 rounded-lg text-sm">
        <i class="fa-solid fa-arrow-left mr-1"></i>Back
      </a>

      <button class="btn px-3 py-2 rounded-lg text-sm" @click="toggleSidebar" title="Toggle sidebar">
        <i class="fa-solid" :class="sidebarCollapsed ? 'fa-angles-right' : 'fa-angles-left'"></i>
      </button>

      <button class="btn px-3 py-2 rounded-lg text-sm" @click="fetchSessions">
        <i class="fa-solid fa-list mr-1"></i>Sessions
      </button>

      <button class="btn-primary px-3 py-2 rounded-lg text-sm shadow-sm" @click="apply(true)">
        <i class="fa-solid fa-rotate-right mr-1"></i>Refresh
      </button>
    </div>
  </div>

  <!-- Main: Sidebar + Chart -->
  <div class="max-w-7xl mx-auto flex gap-4 items-start">

    <!-- Sidebar -->
    <aside class="card transition-all duration-200"
           :class="sidebarCollapsed ? 'w-14 p-2' : 'w-[360px] p-4'">

      <!-- Collapsed View -->
      <div v-if="sidebarCollapsed" class="flex flex-col items-center gap-3">
        <button class="btn w-10 h-10 rounded-xl flex items-center justify-center" @click="toggleSidebar" title="Expand">
          <i class="fa-solid fa-angles-right"></i>
        </button>
        <div class="w-full border-t my-1"></div>
        <button class="btn w-10 h-10 rounded-xl flex items-center justify-center" @click="apply(true)" title="Refresh">
          <i class="fa-solid fa-rotate-right"></i>
        </button>
        <button class="btn w-10 h-10 rounded-xl flex items-center justify-center" @click="resetToDefaultWindow" title="Default window">
          <i class="fa-solid fa-clock-rotate-left"></i>
        </button>
        <div class="w-full border-t my-1"></div>
        <div class="text-[10px] muted text-center leading-tight">
          Sidebar<br/>collapsed
        </div>
      </div>

      <!-- Expanded View -->
      <div v-else>

        <!-- Session card (outside Summary/Controls as requested) -->
        <div class="mb-4">
          <div class="flex items-center justify-between mb-2">
            <div class="font-semibold">Session</div>
            <button class="btn px-2 py-1 rounded-lg text-xs" @click="toggleSidebar" title="Collapse">
              <i class="fa-solid fa-angles-left mr-1"></i>Collapse
            </button>
          </div>

          <label class="text-xs font-semibold muted">Program launch (session id)</label>
          <select v-model="sessionId" @change="onSessionChanged"
                  class="mt-1 w-full border rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-indigo-200">
            <option v-for="s in sessions" :key="s.run_id" :value="s.run_id">{{ s.run_id }}</option>
          </select>

        <div class="mt-2 flex items-center gap-2 text-xs">
          <span class="px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 font-semibold">
            Bounds
          </span>
        
          <!-- Main range text: truncate on overflow -->
          <div class="min-w-0 flex-1">
            <span class="mono text-gray-700 truncate block"
                  :title="sessionRangeText">
              {{ sessionRangeShort }}
            </span>
          </div>

          <span class="px-2 py-0.5 rounded-full bg-gray-100 text-gray-700 mono">
            {{ sessionDurationHuman }}
          </span>
        </div>
        </div>

        <div class="mb-4">
          <div class="font-semibold mb-2">Summary</div>

          <div class="p-3 rounded-xl bg-white border mb-3">
            <div class="text-xs muted">Utilization (RUN_*)</div>
            <div class="mt-1 text-2xl font-bold">{{ (kpi.util*100).toFixed(1) }}%</div>
            <div class="mt-2 h-2 rounded-full bg-gray-100 overflow-hidden">
              <div class="h-full bg-indigo-600" :style="{ width: Math.min(100, kpi.util*100) + '%' }"></div>
            </div>
            <div class="text-[11px] muted mt-2">Total RUN_* duration / window</div>
          </div>

          <div class="p-3 rounded-xl bg-white border mb-3">
            <div class="text-xs muted">Avg latency (completed)</div>
            <div class="mt-1 text-2xl font-bold">{{ kpi.avgLatency.toFixed(2) }}s</div>
            <div class="text-[11px] muted mt-2">Mean of RUN_SUCCESS + RUN_FAIL</div>
          </div>

          <div class="p-3 rounded-xl bg-white border">
            <div class="flex items-center justify-between">
              <div class="text-xs muted">State breakdown (by time)</div>
              <div class="text-xs muted">Segments: <b class="text-gray-700">{{ visibleItems.length }}</b></div>
            </div>
            <div class="mt-3 space-y-2">
              <div v-for="row in breakdown" :key="row.state" class="flex items-center gap-2">
                <div class="w-24 text-xs text-gray-700">{{ row.state }}</div>
                <div class="flex-1 h-2 bg-gray-100 rounded-full overflow-hidden">
                  <div class="h-full" :style="{width: row.percent+'%', background: legend[row.state] || '#93c5fd'}"></div>
                </div>
                <div class="w-10 text-xs muted text-right">{{ row.percent.toFixed(0) }}%</div>
              </div>
            </div>
            <div class="mt-2 text-[11px] muted">
              Computed from segment durations (not counts).
            </div>
          </div>
        </div>

        <!-- Controls -->
        <div>
          <div class="flex items-center justify-between mb-2">
            <div class="font-semibold">Time & Filter</div>
            <button class="btn px-2 py-1 rounded-lg text-xs" @click="resetToDefaultWindow" :disabled="!sessionId">
              Default
            </button>
          </div>

          <div class="p-3 rounded-xl bg-white border mb-4">
            <div class="text-xs muted">Window (active)</div>
            <div class="mt-1 text-sm font-semibold">{{ prettyWindow }}</div>
            <div class="mt-1 text-[11px] muted mono">{{ windowDurationSec }}s</div>
            <div class="mt-2 text-[11px] muted">
              This window drives the chart and all summary metrics.
            </div>
          </div>

          <div class="mb-4">
            <label class="text-xs font-semibold muted">Window selector</label>
            <div class="mt-2">
              <div class="flex items-center justify-between text-[11px] muted">
                <span>Start: {{ (startPct*100).toFixed(0) }}%</span>
                <span>End: {{ (endPct*100).toFixed(0) }}%</span>
              </div>
              <input type="range" min="0" max="100" step="1" v-model.number="startPctUI"
                     @input="onSliderChanged('start')" class="w-full mt-1"/>
              <input type="range" min="0" max="100" step="1" v-model.number="endPctUI"
                     @input="onSliderChanged('end')" class="w-full mt-2"/>
              <div class="text-[11px] muted mt-2">Drag sliders to choose a window inside session.</div>
            </div>
          </div>

          <div class="mb-4">
            <label class="text-xs font-semibold muted">Precise time</label>
            <div class="mt-2 grid grid-cols-1 gap-2">
              <div>
                <div class="text-[11px] muted">From</div>
                <input type="datetime-local" v-model="fromLocal" @change="markDirty"
                       class="mt-1 w-full border rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-indigo-200">
              </div>
              <div>
                <div class="text-[11px] muted">To</div>
                <input type="datetime-local" v-model="toLocal" @change="markDirty"
                       class="mt-1 w-full border rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-indigo-200">
              </div>
            </div>

            <div class="mt-2 flex gap-2">
              <button class="btn px-3 py-2 rounded-lg text-sm w-full" @click="setNowTo" :disabled="!sessionId">To Now</button>
              <button class="btn px-3 py-2 rounded-lg text-sm w-full" @click="fitToData" :disabled="items.length===0">Fit Data</button>
            </div>
          </div>

          <div class="mb-4">
            <label class="text-xs font-semibold muted">Client filter</label>
            <div class="mt-1 flex items-center border rounded-lg px-3 py-2 bg-white focus-within:ring-2 focus-within:ring-indigo-200">
              <i class="fa-solid fa-magnifying-glass text-gray-400 mr-2"></i>
              <input v-model="clientSearch" @input="markDirty"
                     class="w-full text-sm outline-none" placeholder="Type client name...">
            </div>
            <div class="mt-2 text-[11px] muted">Applied after clicking Apply (or Refresh).</div>
          </div>

          <div class="pt-3 border-t">
            <div class="flex items-center justify-between">
              <div class="text-xs" :class="dirty ? 'text-amber-600' : 'muted'">
                <i class="fa-solid fa-circle-info mr-1"></i>{{ dirty ? 'Pending changes' : 'Up to date' }}
              </div>
              <button class="btn-primary px-4 py-2 rounded-lg text-sm shadow-sm"
                      @click="apply(false)" :disabled="!dirty && items.length>0">
                <i class="fa-solid fa-check mr-1"></i>Apply
              </button>
            </div>

            <div class="mt-3 flex items-center justify-between">
              <div class="text-xs muted">Auto refresh</div>
              <div class="flex items-center gap-2">
                <input type="checkbox" v-model="autoRefresh" @change="resetAutoRefresh" />
                <select v-model.number="autoRefreshSec" class="border rounded-lg px-2 py-1 text-sm bg-white" :disabled="!autoRefresh" @change="resetAutoRefresh">
                  <option :value="10">10s</option>
                  <option :value="20">20s</option>
                  <option :value="60">60s</option>
                </select>
              </div>
            </div>

            <div class="text-[11px] muted mt-2">Auto refresh won't override un-applied edits.</div>
          </div>

        </div>
      </div>
    </aside>

    <!-- Chart -->
    <main class="card p-4 flex-1 min-w-0">
      <div class="flex items-center justify-between mb-2">
        <div>
          <div class="font-semibold">Timeline</div>
          <div class="text-xs muted mt-0.5">
            Drag to pan • Use toolbar to zoom • Double click to reset
          </div>
        </div>

        <div class="flex items-center gap-2">
          <button class="btn px-3 py-2 rounded-lg text-sm" @click="toggleCompact">
            <i class="fa-solid fa-compress mr-1"></i>{{ compactMode ? 'Comfort' : 'Compact' }}
          </button>
          <button class="btn px-3 py-2 rounded-lg text-sm" @click="resetView">
            <i class="fa-solid fa-arrows-rotate mr-1"></i>Reset view
          </button>
        </div>
      </div>

      <div v-if="visibleItems.length===0" class="border rounded-xl p-6 text-center text-sm muted bg-gray-50">
        <div class="text-gray-700 font-semibold">No data in this window</div>
        <div class="mt-1">Try "Full" or a wider window. You can also Fit Data.</div>
      </div>

      <div id="timelinePlot" v-show="visibleItems.length>0"></div>
    </main>

  </div>

</div>

<script>
const { createApp } = Vue;

function pad2(n){ return String(n).padStart(2,'0'); }
function toLocalInputString(date){
  const y = date.getFullYear();
  const m = pad2(date.getMonth()+1);
  const d = pad2(date.getDate());
  const hh = pad2(date.getHours());
  const mm = pad2(date.getMinutes());
  return `${y}-${m}-${d}T${hh}:${mm}`;
}
function localInputToEpochSeconds(s){
  if(!s) return null;
  const d = new Date(s);
  const t = d.getTime();
  if(isNaN(t)) return null;
  return Math.floor(t/1000);
}
function clamp(x, a, b){ return Math.max(a, Math.min(b, x)); }

createApp({
  data(){
    const now = new Date();
    const to = new Date(now.getTime());
    const from = new Date(now.getTime() - 3600*1000);

    return {
      sessions: [],
      sessionId: "",

      sessionStart: 0,
      sessionEnd: 0,

      preset: "1h",
      fromLocal: toLocalInputString(from),
      toLocal: toLocalInputString(to),

      startPctUI: 0,
      endPctUI: 100,

      clientSearch: "",

      legend: {
        "RUN_SUCCESS": "rgba(34,197,94,0.88)",
        "RUN_FAIL": "rgba(239,68,68,0.88)",
        "RUNNING": "rgba(245,158,11,0.85)",
        "IDLE_OK": "rgba(79,70,229,0.18)",
        "IDLE_ERROR": "rgba(251,146,60,0.40)",
        "UNAVAILABLE": "rgba(107,114,128,0.55)",
        "UNKNOWN": "rgba(147,197,253,0.55)"
      },

      items: [],

      loading: false,
      lastUpdated: "-",
      dirty: true,

      autoRefresh: false,
      autoRefreshSec: 20,
      timer: null,

      compactMode: false,
      sidebarCollapsed: false,

      uiRevisionKey: "init",

      kpi: { util: 0, avgLatency: 0 },
      
      wasClipped: false
    };
  },

  computed: {
    fromEpoch(){ return localInputToEpochSeconds(this.fromLocal); },
    toEpoch(){ return localInputToEpochSeconds(this.toLocal); },

    sessionRangeText(){
      if(!this.sessionId) return "-";
      const s = new Date(this.sessionStart*1000);
      const e = new Date(this.sessionEnd*1000);
      return `${s.toLocaleString()} → ${e.toLocaleString()}`;
    },

    sessionDurationSec(){
      if(!this.sessionId) return 0;
      return Math.max(0, this.sessionEnd - this.sessionStart);
    },
    
    sessionRangeShort(){
      // Shorter display to avoid wrapping, full text remains in title tooltip
      if(!this.sessionId) return "-";
      const s = new Date(this.sessionStart*1000);
      const e = new Date(this.sessionEnd*1000);
    
      const fmt = (d) => {
        // MM-DD HH:MM:SS
        const mm = String(d.getMonth()+1).padStart(2,'0');
        const dd = String(d.getDate()).padStart(2,'0');
        const hh = String(d.getHours()).padStart(2,'0');
        const mi = String(d.getMinutes()).padStart(2,'0');
        const ss = String(d.getSeconds()).padStart(2,'0');
        return `${mm}-${dd} ${hh}:${mi}:${ss}`;
      };
    
      return `${fmt(s)} → ${fmt(e)}`;
    },
    
    sessionDurationHuman(){
      // 300.233s -> "5m 0.2s" (or "300.2s" for short sessions)
      const sec = Number(this.sessionDurationSec || 0);
      if(sec <= 0) return "0s";
    
      if(sec < 90){
        return `${sec.toFixed(1)}s`;
      }
      const m = Math.floor(sec / 60);
      const s = sec - m*60;
      if(m < 60){
        return `${m}m ${s.toFixed(0)}s`;
      }
      const h = Math.floor(m / 60);
      const mm = m % 60;
      return `${h}h ${mm}m`;
    },

    prettyWindow(){
      if(!this.fromEpoch || !this.toEpoch) return "-";
      const f = new Date(this.fromEpoch*1000);
      const t = new Date(this.toEpoch*1000);
      return `${f.toLocaleString()} → ${t.toLocaleString()}`;
    },

    windowDurationSec(){
      if(!this.fromEpoch || !this.toEpoch) return 0;
      return Math.max(0, this.toEpoch - this.fromEpoch);
    },

    startPct(){ return clamp(this.startPctUI/100, 0, 1); },
    endPct(){ return clamp(this.endPctUI/100, 0, 1); },

    visibleItems(){
      const q = (this.clientSearch || "").toLowerCase().trim();
      return (this.items || []).filter(it => {
        if(!q) return true;
        return String(it.client).toLowerCase().includes(q);
      });
    },

    breakdown(){
      const vis = this.visibleItems;
      const total = vis.reduce((acc, it) => acc + Math.max(0, it.end - it.start), 0) || 1;

      const dur = {};
      vis.forEach(it => {
        const st = it.state || "UNKNOWN";
        const d = Math.max(0, it.end - it.start);
        dur[st] = (dur[st] || 0) + d;
      });

      const order = ["RUN_SUCCESS","RUN_FAIL","RUNNING","IDLE_OK","IDLE_ERROR","UNAVAILABLE","UNKNOWN"];
      return order
        .filter(s => dur[s])
        .map(s => ({ state: s, percent: (dur[s] / total) * 100 }))
        .sort((a,b) => b.percent - a.percent);
    }
  },

  async mounted(){
    await this.fetchSessions();
    if(this.sessionId){
      this.resetToDefaultWindow();
      await this.apply(true);  // Will auto-fix time range if needed
    }
  },

  methods: {
    toggleSidebar(){ 
      this.sidebarCollapsed = !this.sidebarCollapsed;
      setTimeout(() => {
        try { Plotly.Plots.resize("timelinePlot"); } catch(e) {}
      }, 200);
    },

    markDirty(){ this.dirty = true; },

    async fetchSessions(){
      try{
        const res = await fetch("api/runs");
        const data = await res.json();
        this.sessions = data.runs || [];

        if(!this.sessionId && this.sessions.length > 0){
          this.sessionId = this.sessions[0].run_id;
          this._updateSessionMetaFromList();
        }
      }catch(e){
        console.error("fetchSessions", e);
      }
    },

    _updateSessionMetaFromList(){
      const found = this.sessions.find(x => x.run_id === this.sessionId);
      if(!found) return;

      const start = Number(found.start_ts || 0);
      const end = Number(found.end_ts || found.last_heartbeat_ts || Math.floor(Date.now()/1000));

      this.sessionStart = start;
      // Ensure end >= start + 1 to avoid invalid windows on fresh sessions
      this.sessionEnd = Math.max(start + 1, end);
    },

    onSessionChanged(){
      this._updateSessionMetaFromList();
      this.resetToDefaultWindow();
      this.apply(true);
    },

    resetToDefaultWindow(){
      if(!this.sessionId) return;

      const nowEnd = this.sessionEnd;
      const dur = this.sessionDurationSec;

      let fromTs, toTs;
      if(dur > 3600){
        toTs = nowEnd;
        fromTs = Math.max(this.sessionStart, nowEnd - 3600);
        this.preset = "1h";
      } else {
        fromTs = this.sessionStart;
        toTs = nowEnd;
        this.preset = "full";
      }

      this.fromLocal = toLocalInputString(new Date(fromTs*1000));
      this.toLocal = toLocalInputString(new Date(toTs*1000));
      this._syncSlidersFromWindow();
      this.markDirty();
    },

    setPreset(p){
      if(!this.sessionId) return;
      this.preset = p;

      const end = this.sessionEnd;
      const start = this.sessionStart;

      let fromTs = start;
      let toTs = end;

      if(p === "full"){
        fromTs = start; toTs = end;
      } else if(p === "15m"){
        toTs = end; fromTs = Math.max(start, end - 15*60);
      } else if(p === "1h"){
        toTs = end; fromTs = Math.max(start, end - 3600);
      } else if(p === "6h"){
        toTs = end; fromTs = Math.max(start, end - 6*3600);
      }

      this.fromLocal = toLocalInputString(new Date(fromTs*1000));
      this.toLocal = toLocalInputString(new Date(toTs*1000));
      this._syncSlidersFromWindow();
      this.markDirty();
    },

    _syncSlidersFromWindow(){
      const s0 = this.sessionStart, s1 = this.sessionEnd;
      const span = Math.max(1, s1 - s0);

      const fEpoch = this.fromEpoch ?? s0;
      const tEpoch = this.toEpoch ?? s1;

      const f = clamp((fEpoch - s0) / span, 0, 1);
      const t = clamp((tEpoch - s0) / span, 0, 1);

      this.startPctUI = Math.round(f * 100);
      this.endPctUI = Math.round(t * 100);

      if(this.startPctUI > this.endPctUI){
        const tmp = this.startPctUI;
        this.startPctUI = this.endPctUI;
        this.endPctUI = tmp;
      }
    },

    onSliderChanged(which){
      if(!this.sessionId) return;

      if(this.startPctUI > this.endPctUI){
        if(which === "start") this.endPctUI = this.startPctUI;
        else this.startPctUI = this.endPctUI;
      }

      const s0 = this.sessionStart, s1 = this.sessionEnd;
      const span = Math.max(1, s1 - s0);

      const fromTs = Math.floor(s0 + (this.startPctUI/100) * span);
      const toTs = Math.floor(s0 + (this.endPctUI/100) * span);

      this.fromLocal = toLocalInputString(new Date(fromTs*1000));
      this.toLocal = toLocalInputString(new Date(toTs*1000));
      this.markDirty();
    },

    setNowTo(){
      if(!this.sessionId) return;

      const toTs = this.sessionEnd;
      const currentFrom = this.fromEpoch ?? Math.max(this.sessionStart, toTs - 3600);

      this.toLocal = toLocalInputString(new Date(toTs*1000));
      this.fromLocal = toLocalInputString(new Date(clamp(currentFrom, this.sessionStart, toTs)*1000));
      this._syncSlidersFromWindow();
      this.markDirty();
    },

    fitToData(){
      if(!this.items || this.items.length===0) return;
      let minS = Infinity, maxE = 0;
      this.items.forEach(it => {
        minS = Math.min(minS, it.start);
        maxE = Math.max(maxE, it.end);
      });
      if(minS!==Infinity && maxE>0){
        minS = clamp(minS, this.sessionStart, this.sessionEnd);
        maxE = clamp(maxE, this.sessionStart, this.sessionEnd);

        this.fromLocal = toLocalInputString(new Date(minS*1000));
        this.toLocal = toLocalInputString(new Date(maxE*1000));
        this._syncSlidersFromWindow();
        this.markDirty();
      }
    },

    toggleCompact(){
      this.compactMode = !this.compactMode;
      this.renderPlot(true);
    },

    resetView(){
      try { Plotly.relayout("timelinePlot", { "xaxis.autorange": true, "yaxis.autorange": true }); } catch(e){}
    },

    resetAutoRefresh(){
      if(this.timer){
        clearInterval(this.timer);
        this.timer = null;
      }
      if(this.autoRefresh){
        this.timer = setInterval(() => {
          if(!this.dirty) this.apply(true);
        }, Math.max(5, this.autoRefreshSec) * 1000);
      }
    },

    async apply(force){
      if(!force && !this.dirty) return;

      if(!this.sessionId){
        await this.fetchSessions();
        if(!this.sessionId) return;
      }

      this._updateSessionMetaFromList();


      // Auto-fix invalid timerange on startup (no alert)
      let fromTs = this.fromEpoch;
      let toTs = this.toEpoch;

      if(fromTs === null || toTs === null){
        // Default to last 1h within session
        toTs = this.sessionEnd;
        fromTs = Math.max(this.sessionStart, toTs - 3600);
      }

      // Keep original user input for clip detection
      const rawFrom = fromTs;
      const rawTo = toTs;

      // Clip to session bounds
      fromTs = clamp(fromTs, this.sessionStart, this.sessionEnd);
      toTs = clamp(toTs, this.sessionStart, this.sessionEnd);

      // Mark if we had to adjust into session bounds
      this.wasClipped = (rawFrom !== fromTs) || (rawTo !== toTs);
    
      if(toTs <= fromTs){
        // Ensure at least 1s window
        toTs = Math.min(this.sessionEnd, fromTs + 1);
        if(toTs <= fromTs) toTs = fromTs + 1;
        // Optional: if this adjustment changes, it is also a "clip/adjust"
        this.wasClipped = true;
      }

      // Reflect clipped values back to inputs
      this.fromLocal = toLocalInputString(new Date(fromTs*1000));
      this.toLocal = toLocalInputString(new Date(toTs*1000));
      this._syncSlidersFromWindow();

      const qs = new URLSearchParams({
        run_id: this.sessionId,
        from: String(fromTs),
        to: String(toTs)
      });

      this.loading = true;
      try{
        const res = await fetch("api/timeline?" + qs.toString());
        const data = await res.json();

        this.items = data.items || [];
        if(data.legend){
          this.legend = { ...this.legend, ...data.legend };
        }

        this.lastUpdated = new Date().toLocaleTimeString();
        this.dirty = false;

        this.uiRevisionKey = this.sessionId + ":" + String(fromTs) + ":" + String(toTs);

        this.calcKPI();
        this.renderPlot(false);
        this.resetAutoRefresh();
      }catch(e){
        console.error("apply/fetch timeline", e);
      }finally{
        this.loading = false;
      }
    },

    calcKPI(){
      const vis = this.visibleItems;
      const win = Math.max(1, (this.toEpoch||0) - (this.fromEpoch||0));

      let runDur = 0;
      let completed = [];

      vis.forEach(it => {
        const d = Math.max(0, it.end - it.start);
        if(String(it.state||"").startsWith("RUN_")) runDur += d;
        if(it.state==="RUN_SUCCESS" || it.state==="RUN_FAIL") completed.push(d);
      });

      this.kpi.util = runDur / win;
      this.kpi.avgLatency = completed.length ? (completed.reduce((a,b)=>a+b,0)/completed.length) : 0;
    },

    renderPlot(onlyRelayout){
      const items = this.visibleItems;
      if(!items || items.length===0){
        try { Plotly.purge("timelinePlot"); } catch(e){}
        return;
      }

      const byState = {};
      items.forEach(it => {
        const st = it.state || "UNKNOWN";
        (byState[st] ||= []).push(it);
      });

      const clients = [...new Set(items.map(x => x.client))].sort();
      const height = Math.max(520, Math.min(920, 260 + clients.length * (this.compactMode ? 20 : 26)));

      const traces = Object.keys(byState).map(st => {
        const segs = byState[st];
        const isIdle = (st==="IDLE_OK" || st==="IDLE_ERROR");

        return {
          type: "bar",
          orientation: "h",
          name: st,
          y: segs.map(s => s.client),
          base: segs.map(s => new Date(s.start*1000)),
          x: segs.map(s => Math.max(0, (s.end - s.start)*1000)),
          hoverinfo: "text",
          hovertext: segs.map(s => {
            const dur = (s.end - s.start).toFixed(2);
            const start = new Date(s.start*1000).toLocaleString();
            const end = new Date(s.end*1000).toLocaleString();
            const modelText = s.model ? `<br>${s.model}` : "";
            return `${s.client}<br><b>${st}</b>${modelText}<br>${start} → ${end}<br>Dur: ${dur}s`;
          }),
          marker: {
            color: this.legend[st] || "rgba(147,197,253,0.55)",
            line: { color: isIdle ? "rgba(148,163,184,0.25)" : "rgba(255,255,255,0.45)", width: isIdle ? 0.8 : 0.5 }
          }
        };
      });

      const layout = {
        height,
        margin: { l: 160, r: 18, t: 8, b: 55 },
        paper_bgcolor: "white",
        plot_bgcolor: "white",
        barmode: "overlay",
        bargap: this.compactMode ? 0.72 : 0.64,
        legend: { orientation: "h", x: 0, y: -0.18 },
        uirevision: this.uiRevisionKey,
        xaxis: {
          type: "date",
          title: { text: "Time", standoff: 8 },
          gridcolor: "rgba(148,163,184,0.14)",
          zeroline: false
        },
        yaxis: {
          title: { text: "" },
          automargin: true,
          categoryorder: "array",
          categoryarray: clients
        },
        dragmode: "pan",
        hovermode: "closest"
      };

      const config = {
        responsive: true,
        displayModeBar: true,
        displaylogo: false,
        scrollZoom: true,
        doubleClick: "reset",
        modeBarButtonsToRemove: [
          "select2d","lasso2d","autoScale2d",
          "hoverCompareCartesian","hoverClosestCartesian",
          "toggleSpikelines"
        ]
      };

      if(onlyRelayout){
        try { Plotly.relayout("timelinePlot", layout); }
        catch(e){ Plotly.react("timelinePlot", traces, layout, config); }
        return;
      }
      Plotly.react("timelinePlot", traces, layout, config);
    }
  },

  watch: {
    clientSearch(){
      this.calcKPI();
      this.renderPlot(false);
    },
  }
}).mount("#app");
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

        @bp.route('/timeline', methods=['GET'])
        @maybe_wrap
        def timeline_view():
            """Serve embedded Timeline page."""
            return Response(FRONTEND_TIMELINE_HTML, mimetype='text/html')

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

        @bp.route('/api/runs', methods=['GET'])
        @maybe_wrap
        def api_runs():
            """List recent run_id for timeline selection (via state logger)."""
            state_logger = self.manager.get_state_logger() if hasattr(self.manager, "get_state_logger") else None
            if not state_logger:
                return jsonify({"runs": [], "warning": "state_logger is not enabled"}), 200

            try:
                return jsonify(state_logger.get_run_list(limit=50))
            except Exception as e:
                return jsonify({"runs": [], "error": str(e)}), 500

        @bp.route('/api/timeline', methods=['GET'])
        @maybe_wrap
        def api_timeline():
            """Return timeline intervals for plotting (via state logger)."""
            state_logger = self.manager.get_state_logger() if hasattr(self.manager, "get_state_logger") else None
            if not state_logger:
                return jsonify({"items": [], "warning": "state_logger is not enabled"}), 200

            run_id = (request.args.get("run_id") or "").strip()
            if not run_id:
                return jsonify({"error": "Missing run_id"}), 400

            try:
                from_ts = float(request.args.get("from", 0))
                to_ts = float(request.args.get("to", time.time()))
            except Exception:
                return jsonify({"error": "Invalid from/to"}), 400

            client_name = (request.args.get("client") or "").strip() or None

            try:
                data = state_logger.query_timeline(run_id=run_id, from_ts=from_ts, to_ts=to_ts, client_name=client_name)
                return jsonify(data)
            except Exception as e:
                return jsonify({"error": str(e), "items": []}), 500

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
