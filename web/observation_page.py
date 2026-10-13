"""AI 调用观测页面；仅消费通用观测 API。"""

OBSERVATION_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>AI 调用观测</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
  <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
  <style>
    [v-cloak]{display:none}.mono{font-variant-numeric:tabular-nums}
    .card{background:#fff;border:1px solid #e7e5e4;border-radius:18px;box-shadow:0 12px 30px rgba(28,25,23,.045)}
    .soft-grid{background-image:linear-gradient(#e7e5e455 1px,transparent 1px),linear-gradient(90deg,#e7e5e455 1px,transparent 1px);background-size:28px 28px}
    .bar{transition:width .35s ease}.dot{width:8px;height:8px;border-radius:999px;display:inline-block}
  </style>
</head>
<body class="bg-stone-50 text-stone-800">
<div id="app" v-cloak class="min-h-screen">
  <header class="border-b border-stone-200 bg-white/90 sticky top-0 z-10 backdrop-blur">
    <div class="max-w-7xl mx-auto px-5 py-4 flex flex-wrap gap-4 items-center justify-between">
      <div class="flex items-center gap-3">
        <div class="h-10 w-10 rounded-xl bg-stone-900 text-white grid place-items-center"><i class="fa-solid fa-wave-square"></i></div>
        <div><h1 class="font-bold text-xl">AI 调用观测</h1><p class="text-xs text-stone-500">流量、可靠性与能力反馈</p></div>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <select v-model.number="hours" @change="load" class="border border-stone-300 bg-white rounded-xl px-3 py-2 text-sm">
          <option :value="1">最近 1 小时</option><option :value="6">最近 6 小时</option>
          <option :value="24">最近 24 小时</option><option :value="168">最近 7 天</option>
          <option :value="720">最近 30 天</option>
        </select>
        <select v-model="dimension" @change="load" class="border border-stone-300 bg-white rounded-xl px-3 py-2 text-sm">
          <option value="model">按模型</option><option value="provider">按服务商</option>
          <option value="account_alias">按账号</option><option value="client_name">按 Client</option>
        </select>
        <button @click="load" :disabled="loading" class="rounded-xl px-4 py-2 text-sm bg-stone-900 text-white hover:bg-stone-700 disabled:opacity-50">
          <i class="fa-solid fa-rotate-right mr-1" :class="{'fa-spin':loading}"></i>刷新
        </button>
        <a href="./" class="rounded-xl px-4 py-2 text-sm border border-stone-300 bg-white hover:bg-stone-100">返回 Client</a>
      </div>
    </div>
  </header>

  <main class="max-w-7xl mx-auto p-5 space-y-5">
    <div v-if="error" class="rounded-xl border border-red-200 bg-red-50 text-red-700 p-4">{{ error }}</div>
    <div v-if="!enabled && !loading" class="card p-10 text-center">
      <div class="text-3xl text-stone-300 mb-3"><i class="fa-solid fa-chart-simple"></i></div>
      <div class="font-semibold">调用观测尚未启用</div>
      <p class="text-sm text-stone-500 mt-2">为 AIClientManager 配置 CallObservationSQLiteLogger 后，此页会自动显示数据。</p>
    </div>

    <template v-if="enabled">
      <section class="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <div class="card p-5"><div class="text-xs text-stone-500">调用</div><div class="text-3xl font-bold mono mt-2">{{ fmt(overall.request_count) }}</div><div class="text-xs text-stone-400 mt-1">真实进入 Client</div></div>
        <div class="card p-5"><div class="text-xs text-stone-500">成功率</div><div class="text-3xl font-bold mono mt-2" :class="rateColor(overall.success_rate)">{{ pct(overall.success_rate) }}</div><div class="text-xs text-stone-400 mt-1">{{ fmt(overall.success_count) }} 次成功</div></div>
        <div class="card p-5"><div class="text-xs text-stone-500">Token</div><div class="text-3xl font-bold mono mt-2">{{ compact(totalTokens) }}</div><div class="text-xs text-stone-400 mt-1">入 {{ compact(overall.input_tokens) }} · 出 {{ compact(overall.output_tokens) }}</div></div>
        <div class="card p-5"><div class="text-xs text-stone-500">逻辑流量</div><div class="text-3xl font-bold mono mt-2">{{ bytes(totalBytes) }}</div><div class="text-xs text-stone-400 mt-1">JSON UTF-8 载荷</div></div>
        <div class="card p-5 col-span-2 lg:col-span-1"><div class="text-xs text-stone-500">平均响应</div><div class="text-3xl font-bold mono mt-2">{{ duration(overall.average_elapsed_ms) }}</div><div class="text-xs text-stone-400 mt-1">实报 Token {{ pct(reportedRatio) }}</div></div>
      </section>

      <section class="grid lg:grid-cols-3 gap-5">
        <div class="card p-5 lg:col-span-2">
          <div class="flex justify-between items-end mb-5"><div><h2 class="font-bold">调用趋势</h2><p class="text-xs text-stone-500 mt-1">柱高为调用次数，颜色表示成功率</p></div><div class="text-xs text-stone-400">{{ updatedAt }}</div></div>
          <div v-if="series.length" class="h-64 flex items-end gap-1 soft-grid rounded-xl px-3 pt-5 pb-8 overflow-hidden">
            <div v-for="point in series" :key="point.time_bucket" class="group flex-1 min-w-[5px] h-full flex items-end relative">
              <div class="bar w-full rounded-t-sm" :class="point.success_rate >= .95 ? 'bg-emerald-500' : point.success_rate >= .8 ? 'bg-amber-400' : 'bg-red-400'" :style="{height:barHeight(point.request_count)}"></div>
              <div class="hidden group-hover:block absolute z-20 bottom-3 left-1/2 -translate-x-1/2 whitespace-nowrap bg-stone-900 text-white text-xs rounded-lg px-3 py-2 shadow-xl">
                {{ bucketLabel(point.time_bucket) }}<br>{{ point.request_count }} 次 · 成功 {{ pct(point.success_rate) }}
              </div>
            </div>
          </div>
          <div v-else class="h-64 grid place-items-center text-sm text-stone-400">所选时段暂无调用</div>
        </div>

        <div class="card p-5">
          <h2 class="font-bold">数据可信度</h2><p class="text-xs text-stone-500 mt-1">区分服务商实报与本地估算</p>
          <div class="mt-7 flex justify-center"><div class="h-36 w-36 rounded-full grid place-items-center" :style="donutStyle">
            <div class="h-24 w-24 bg-white rounded-full grid place-items-center text-center"><div><div class="font-bold text-xl mono">{{ pct(reportedRatio) }}</div><div class="text-[11px] text-stone-500">实报覆盖</div></div></div>
          </div></div>
          <div class="mt-6 space-y-2 text-sm">
            <div class="flex justify-between"><span><i class="dot bg-indigo-600 mr-2"></i>服务商实报</span><b class="mono">{{ fmt(overall.reported_token_calls) }}</b></div>
            <div class="flex justify-between"><span><i class="dot bg-stone-200 mr-2"></i>本地估算</span><b class="mono">{{ fmt((overall.request_count||0)-(overall.reported_token_calls||0)) }}</b></div>
            <div class="flex justify-between pt-2 border-t text-stone-500"><span>已反馈质量</span><span>{{ fmt(overall.feedback_count) }} 次</span></div>
          </div>
        </div>
      </section>

      <section class="card overflow-hidden">
        <div class="p-5 border-b border-stone-200"><h2 class="font-bold">{{ dimensionTitle }}表现</h2><p class="text-xs text-stone-500 mt-1">同时比较用量、稳定性和响应速度</p></div>
        <div class="overflow-x-auto"><table class="w-full text-sm">
          <thead class="bg-stone-50 text-xs text-stone-500"><tr><th class="text-left px-5 py-3">名称</th><th class="text-right px-4 py-3">调用</th><th class="text-right px-4 py-3">成功率</th><th class="text-right px-4 py-3">Token</th><th class="text-right px-4 py-3">流量</th><th class="text-right px-4 py-3">平均响应</th><th class="text-right px-5 py-3">质量反馈</th></tr></thead>
          <tbody class="divide-y divide-stone-100"><tr v-for="row in breakdown" :key="row[dimension]" class="hover:bg-stone-50">
            <td class="px-5 py-4 font-medium">{{ row[dimension] || '未标记' }}</td><td class="px-4 py-4 text-right mono">{{ fmt(row.request_count) }}</td>
            <td class="px-4 py-4 text-right"><span class="px-2 py-1 rounded-full text-xs" :class="rateBadge(row.success_rate)">{{ pct(row.success_rate) }}</span></td>
            <td class="px-4 py-4 text-right mono">{{ compact((row.input_tokens||0)+(row.output_tokens||0)) }}</td><td class="px-4 py-4 text-right mono">{{ bytes((row.logical_input_bytes||0)+(row.logical_output_bytes||0)) }}</td>
            <td class="px-4 py-4 text-right mono">{{ duration(row.average_elapsed_ms) }}</td><td class="px-5 py-4 text-right mono">{{ row.feedback_count ? pct(row.feedback_valid_rate) : '—' }}</td>
          </tr><tr v-if="!breakdown.length"><td colspan="7" class="p-8 text-center text-stone-400">暂无数据</td></tr></tbody>
        </table></div>
      </section>

      <section class="card overflow-hidden">
        <div class="p-5 border-b border-stone-200"><h2 class="font-bold">最近调用</h2><p class="text-xs text-stone-500 mt-1">仅显示元数据，不保存 Prompt 与回复正文</p></div>
        <div class="overflow-x-auto"><table class="w-full text-sm">
          <thead class="bg-stone-50 text-xs text-stone-500"><tr><th class="text-left px-5 py-3">时间</th><th class="text-left px-4 py-3">服务 / 模型</th><th class="text-left px-4 py-3">账号</th><th class="text-right px-4 py-3">Token</th><th class="text-right px-4 py-3">响应</th><th class="text-right px-5 py-3">结果</th></tr></thead>
          <tbody class="divide-y divide-stone-100"><tr v-for="row in recent" :key="row.request_id">
            <td class="px-5 py-3 whitespace-nowrap text-stone-500">{{ localTime(row.started_at) }}</td><td class="px-4 py-3"><div class="font-medium">{{ row.provider || '未标记' }}</div><div class="text-xs text-stone-500">{{ row.model || row.client_name }}</div></td>
            <td class="px-4 py-3 text-stone-600">{{ row.account_alias || '—' }}</td><td class="px-4 py-3 text-right mono">{{ compact(callTokens(row)) }} <span class="text-[10px] text-stone-400">{{ row.token_source==='reported'?'实报':'估算' }}</span></td>
            <td class="px-4 py-3 text-right mono">{{ duration(row.elapsed_ms) }}</td><td class="px-5 py-3 text-right"><span v-if="row.success" class="text-emerald-700">成功</span><span v-else class="text-red-600" :title="row.error_code || row.error_type">失败 · {{ row.error_code || row.error_type || '未知' }}</span></td>
          </tr><tr v-if="!recent.length"><td colspan="6" class="p-8 text-center text-stone-400">暂无数据</td></tr></tbody>
        </table></div>
      </section>
    </template>
  </main>
</div>
<script>
const {createApp}=Vue;
createApp({
 data(){return{hours:24,dimension:'model',enabled:false,loading:false,error:'',overall:{},series:[],breakdown:[],recent:[],updatedAt:'—'}},
 computed:{
  totalTokens(){return (this.overall.input_tokens||0)+(this.overall.output_tokens||0)},
  totalBytes(){return (this.overall.logical_input_bytes||0)+(this.overall.logical_output_bytes||0)},
  reportedRatio(){const n=this.overall.request_count||0;return n?(this.overall.reported_token_calls||0)/n:0},
  donutStyle(){const p=Math.round(this.reportedRatio*100);return{background:`conic-gradient(#4f46e5 0 ${p}%,#e7e5e4 ${p}% 100%)`}},
  dimensionTitle(){return{model:'模型',provider:'服务商',account_alias:'账号',client_name:'Client'}[this.dimension]||'维度'},
  maxSeries(){return Math.max(1,...this.series.map(x=>x.request_count||0))}
 },
 mounted(){this.load()},
 methods:{
  async load(){this.loading=true;this.error='';try{const r=await fetch(`api/observations?hours=${this.hours}&dimension=${encodeURIComponent(this.dimension)}&limit=40`);const d=await r.json();if(!r.ok)throw new Error(d.error||r.statusText);this.enabled=!!d.enabled;this.overall=d.overall||{};this.series=d.series||[];this.breakdown=d.breakdown||[];this.recent=d.recent||[];this.updatedAt=new Date().toLocaleString()}catch(e){this.error=e.message||String(e)}finally{this.loading=false}},
  fmt(v){return Number(v||0).toLocaleString()},compact(v){v=Number(v||0);if(v>=1e9)return(v/1e9).toFixed(1)+'B';if(v>=1e6)return(v/1e6).toFixed(1)+'M';if(v>=1e3)return(v/1e3).toFixed(1)+'K';return String(Math.round(v))},
  bytes(v){v=Number(v||0);if(v>=1073741824)return(v/1073741824).toFixed(1)+' GB';if(v>=1048576)return(v/1048576).toFixed(1)+' MB';if(v>=1024)return(v/1024).toFixed(1)+' KB';return Math.round(v)+' B'},
  pct(v){return v===null||v===undefined?'—':(Number(v)*100).toFixed(1)+'%'},duration(v){v=Number(v||0);return v>=1000?(v/1000).toFixed(2)+' s':Math.round(v)+' ms'},
  rateColor(v){return v>=.95?'text-emerald-600':v>=.8?'text-amber-600':'text-red-600'},rateBadge(v){return v>=.95?'bg-emerald-50 text-emerald-700':v>=.8?'bg-amber-50 text-amber-700':'bg-red-50 text-red-700'},
  barHeight(v){return Math.max(3,(Number(v||0)/this.maxSeries)*100)+'%'},bucketLabel(v){return String(v||'').replace('T',' ').replace('Z','')},localTime(v){return new Date(Number(v)*1000).toLocaleString()},
  callTokens(r){return (r.reported_input_tokens??r.estimated_input_tokens??0)+(r.reported_output_tokens??r.estimated_output_tokens??0)}
 }
}).mount('#app');
</script>
</body></html>
"""
