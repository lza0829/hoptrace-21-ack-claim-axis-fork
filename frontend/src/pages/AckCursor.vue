<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const proofs = ref([])
const pending = ref([])
const demo = ref(null)
const err = ref('')
const loading = ref(false)

async function loadAll() {
  loading.value = true
  err.value = ''
  try {
    const [p, q] = await Promise.all([api.forkProofs(), api.pendingHops()])
    proofs.value = p
    pending.value = q
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    loading.value = false
  }
}

async function runDemo() {
  err.value = ''
  try {
    demo.value = await api.ackOrderingDemo()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

onMounted(loadAll)
</script>

<template>
  <div class="page-card">
    <h1>ACK 游标与分叉证明</h1>
    <p class="lead">
      每运单持久 ACK 游标与正式物化进度；正式轴只含业务序号 ≤ ACK 的点，超 ACK 点落待确认区。
      ACK 下调整笔拒绝。本页只读证明 + 顺序对照演示，推进入口在测绘板运单检查器。
    </p>
    <p v-if="err" class="muted">{{ err }}</p>

    <h2 class="block-title">ACK 分叉证明（只读）</h2>
    <table class="table">
      <thead>
        <tr><th>运单</th><th>ack_seq</th><th>正式最大序号</th><th>正式点数</th><th>超ACK正式点</th><th>待确认键集合</th><th>结论</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in proofs" :key="r.waybill_id">
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.ack_seq }}</td>
          <td>{{ r.formal_max_seq }}</td>
          <td>{{ r.formal_count }}</td>
          <td :class="r.over_ack_formal_count ? 'ack-bad' : 'ack-ok'">{{ r.over_ack_formal_count }}</td>
          <td>{{ r.pending_keys.join(', ') || '∅' }}</td>
          <td class="conclusion">{{ r.conclusion }}</td>
        </tr>
        <tr v-if="!proofs.length && !loading"><td colspan="7" class="muted">暂无证明</td></tr>
      </tbody>
    </table>

    <h2 class="block-title">待 ACK 区</h2>
    <table class="table">
      <thead>
        <tr><th>hop</th><th>运单</th><th>seq</th><th>事件</th><th>地点</th><th>滞留原因</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="p in pending" :key="p.id" class="row-pending">
          <td>{{ p.id }}</td>
          <td>{{ p.waybill_id }}</td>
          <td>{{ p.seq ?? '—' }}</td>
          <td>{{ p.event_code }}</td>
          <td>{{ p.location_code }}</td>
          <td>{{ p.held_reason === 'missing_seq' ? '无业务序号' : '超 ACK' }}</td>
          <td>{{ p.created_at }}</td>
        </tr>
        <tr v-if="!pending.length"><td colspan="7" class="muted">待 ACK 区为空</td></tr>
      </tbody>
    </table>

    <h2 class="block-title">顺序对照：先装载后 ACK vs 先 ACK 后装载</h2>
    <p class="muted">同一批 {{ demo?.hops?.length ?? 4 }} 个点、同一最终 ACK；演示在独立临时库运行，不影响主库。</p>
    <button class="btn" @click="runDemo">跑顺序对照演示</button>

    <div v-if="demo" class="demo-grid">
      <div v-for="s in demo.scenarios" :key="s.waybill_id" class="demo-col">
        <h3>{{ s.name }} <span class="mono">{{ s.waybill_id }}</span></h3>
        <div v-for="st in s.steps" :key="st.step" class="demo-step">
          <div class="demo-step-head">{{ st.step }}</div>
          <div class="muted mono small">
            ACK={{ st.proof.ack_seq }} · 正式={{ st.proof.formal_count }} ·
            正式最大={{ st.proof.formal_max_seq }} · 待确认=[{{ st.proof.pending_keys.join(', ') }}]
          </div>
          <div v-if="st.action === 'downgrade'" :class="st.result.rejected ? 'ack-bad' : 'ack-bad'">
            {{ st.result.rejected ? '↓ 下调已整笔拒绝（409），已正式点与已写 ACK 未改' : '异常' }}
          </div>
        </div>
      </div>
    </div>
    <div v-if="demo" class="demo-verdict">
      <b>可区分性结论：</b>{{ demo.distinction.verdict }}
    </div>
  </div>
</template>
