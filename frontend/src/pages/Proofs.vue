<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const receipts = ref([])
const audits = ref([])
const err = ref('')

onMounted(async () => {
  try {
    const [p, r, a] = await Promise.all([
      api.proofs(), api.promotionProofs(), api.tearAudits(),
    ])
    rows.value = p
    receipts.value = r
    audits.value = a
  } catch (e) {
    err.value = String(e.message || e)
  }
})

const tearCount = computed(() => audits.value.filter((a) => a.tear).length)
</script>

<template>
  <div class="page-card">
    <h1>证明</h1>
    <p class="lead">状态投影证明列表，与时间线末 hop 同源。</p>
    <p v-if="err" class="muted">{{ err }}</p>
    <table class="table">
      <thead>
        <tr><th>id</th><th>运单</th><th>末 hop</th><th>映射码</th><th>投影</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.last_hop_id }}</td>
          <td>{{ r.mapped_code }}</td>
          <td>{{ r.proj_value }}</td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="6" class="muted">暂无证明</td></tr>
      </tbody>
    </table>

    <h2 class="block-title">晋升回执与撕裂核证</h2>
    <p class="muted small">
      回执来自独立晋升对照 / 撕裂证明表，与待确认页、测绘板共用同一份持久化数据：
      晋升点必须可查、正式轴点数必须增、投影不得停在晋升前、待确认不得被无回执清空。
    </p>
    <p :class="tearCount ? 'ack-bad' : 'ack-ok'">
      {{ tearCount ? `${tearCount} 个运单存在晋升撕裂` : '全部运单无晋升撕裂' }}
    </p>

    <table class="table">
      <thead>
        <tr><th>运单</th><th>ACK</th><th>正式点数</th><th>待确认键</th><th>投影</th><th>核证结论</th></tr>
      </thead>
      <tbody>
        <tr v-for="a in audits" :key="a.waybill_id">
          <td>{{ a.waybill_id }}</td>
          <td>{{ a.ack_seq }}</td>
          <td>{{ a.formal_count }}</td>
          <td>{{ a.pending_keys.join(', ') || '∅' }}</td>
          <td>{{ a.projection }}</td>
          <td :class="a.tear ? 'ack-bad' : 'ack-ok'">{{ a.verdict }}</td>
        </tr>
        <tr v-if="!audits.length"><td colspan="6" class="muted">暂无核证行</td></tr>
      </tbody>
    </table>

    <table class="table" style="margin-top:12px">
      <thead>
        <tr>
          <th>回执</th><th>运单</th><th>ACK 前→后</th><th>晋升 id 列表</th>
          <th>正式点数 前→后</th><th>投影 前→后</th><th>时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in receipts" :key="r.promo_id">
          <td>{{ r.promo_id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.previous_ack_seq }} → {{ r.ack_seq }}</td>
          <td>{{ r.promoted_ids.join(', ') || '∅' }}</td>
          <td>{{ r.formal_count_before }} → {{ r.formal_count_after }}</td>
          <td>{{ r.projection_before }} → {{ r.projection_after }}</td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!receipts.length"><td colspan="7" class="muted">暂无晋升回执</td></tr>
      </tbody>
    </table>
  </div>
</template>
