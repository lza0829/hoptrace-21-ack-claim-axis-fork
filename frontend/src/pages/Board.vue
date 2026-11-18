<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'

const waybills = ref([])
const selected = ref(null)
const detail = ref(null)
const overview = ref(null)
const err = ref('')
const ackInput = ref(0)
const busy = ref(false)
const ackMsg = ref('')

async function loadList() {
  overview.value = await api.overview()
  waybills.value = await api.waybills()
  if (!selected.value && waybills.value.length) {
    selected.value = waybills.value[0].id
  }
}

async function loadDetail(id) {
  if (!id) return
  detail.value = await api.timeline(id)
  ackInput.value = detail.value?.ack_cursor?.ack_seq ?? 0
  ackMsg.value = ''
}

async function advanceAck() {
  if (!selected.value) return
  busy.value = true
  err.value = ''
  ackMsg.value = ''
  try {
    const target = Number(ackInput.value)
    const r = await api.advanceAck(selected.value, target)
    ackMsg.value = r.noop
      ? `ACK 已为 ${r.ack_seq}，无需推进`
      : `ACK 已推进至 ${r.ack_seq}，晋升 ${r.promoted.length} 个待确认点`
    await Promise.all([loadDetail(selected.value), loadList()])
    ackInput.value = target
  } catch (e) {
    err.value = `${e.status || ''} ${e.message}`.trim()
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  try {
    await loadList()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(selected, (id) => {
  loadDetail(id).catch((e) => { err.value = String(e.message || e) })
})

const hops = computed(() => detail.value?.hops || [])
const pending = computed(() => detail.value?.pending_hops || [])
const allPoints = computed(() => detail.value?.all_points || [])
const proof = computed(() => detail.value?.proof)
const fork = computed(() => detail.value?.fork_proof)
const cursor = computed(() => detail.value?.ack_cursor)
</script>

<template>
  <div>
    <p v-if="err" class="muted">{{ err }}</p>
    <div class="stat-row" v-if="overview">
      <div class="stat"><b>{{ overview.waybills }}</b><span>运单</span></div>
      <div class="stat"><b>{{ overview.hops_primary }}</b><span>正式 hop（≤ACK）</span></div>
      <div class="stat"><b>{{ overview.hops_pending_ack }}</b><span>待 ACK 点</span></div>
      <div class="stat"><b>{{ overview.hops_sidecar }}</b><span>旁路</span></div>
      <div class="stat"><b>{{ overview.quarantine }}</b><span>隔离</span></div>
    </div>

    <div class="tri">
      <aside class="pane waybill-pane">
        <h2>运单索引</h2>
        <button
          v-for="w in waybills"
          :key="w.id"
          class="wb-item"
          :class="{ active: selected === w.id }"
          @click="selected = w.id"
        >
          <span class="id">{{ w.id }}</span>
          <span class="title">{{ w.title }}</span>
          <span class="st">{{ w.status_proj }}</span>
          <span class="ack-chip">ACK {{ w.ack_seq ?? 0 }} · 正式 {{ w.formal_max_seq ?? 0 }} · 待 {{ w.pending_count ?? 0 }}</span>
        </button>
      </aside>

      <section class="pane axis-pane">
        <div class="axis-head">
          <h1>{{ detail?.waybill?.title || '时间线' }}</h1>
          <span>{{ selected }}</span>
        </div>

        <h2 class="axis-sub">正式有效轴（业务序号 ≤ ACK）</h2>
        <div class="v-axis" v-if="hops.length">
          <div class="hop-node" v-for="h in hops" :key="h.id">
            <div class="ts">{{ h.event_ts }} · seq {{ h.seq }}</div>
            <div class="code">{{ h.event_code }}</div>
            <div class="loc">{{ h.location_code }} · {{ h.id }}</div>
          </div>
        </div>
        <p v-else class="muted">无正式轨迹点（全部点仍在待 ACK 区）</p>

        <h2 class="axis-sub pending-head">待 ACK 区（超 ACK，不得进正式轴）</h2>
        <table class="table" v-if="pending.length">
          <thead><tr><th>hop</th><th>seq</th><th>事件</th><th>地点</th><th>滞留原因</th></tr></thead>
          <tbody>
            <tr v-for="p in pending" :key="p.id" class="row-pending">
              <td>{{ p.id }}</td>
              <td>{{ p.seq ?? '—' }}</td>
              <td>{{ p.event_code }}</td>
              <td>{{ p.location_code }}</td>
              <td>{{ p.held_reason === 'missing_seq' ? '无业务序号' : '超 ACK' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">待 ACK 区为空</p>
      </section>

      <aside class="pane inspect-pane">
        <h2>检查器</h2>
        <dl class="kv" v-if="detail?.waybill">
          <dt>运单</dt><dd>{{ detail.waybill.id }}</dd>
          <dt>投影状态</dt><dd>{{ detail.waybill.status_proj }}</dd>
          <dt>创建</dt><dd>{{ detail.waybill.created_at }}</dd>
        </dl>

        <h2>ACK 游标推进</h2>
        <dl class="kv" v-if="cursor">
          <dt>当前 ACK</dt><dd>{{ cursor.ack_seq }}</dd>
          <dt>正式最大序号</dt><dd>{{ cursor.formal_max_seq }}</dd>
          <dt>版本</dt><dd>v{{ cursor.version }}</dd>
        </dl>
        <div class="ack-control">
          <label class="muted" for="ack-input">目标 ACK 序号（仅允许上调）</label>
          <input id="ack-input" v-model.number="ackInput" type="number" min="0" class="ack-input" />
          <button class="btn" :disabled="busy" @click="advanceAck">
            {{ busy ? '推进中…' : '推进 ACK / 晋升待确认' }}
          </button>
          <p v-if="ackMsg" class="ack-ok">{{ ackMsg }}</p>
        </div>

        <h2>ACK 分叉证明</h2>
        <dl class="kv" v-if="fork">
          <dt>ack_seq</dt><dd>{{ fork.ack_seq }}</dd>
          <dt>正式最大序号</dt><dd>{{ fork.formal_max_seq }}</dd>
          <dt>正式点数</dt><dd>{{ fork.formal_count }}</dd>
          <dt>超 ACK 正式点</dt>
          <dd :class="fork.over_ack_formal_count ? 'ack-bad' : 'ack-ok'">{{ fork.over_ack_formal_count }}</dd>
          <dt>待确认键集合</dt><dd>{{ fork.pending_keys.join(', ') || '∅' }}</dd>
          <dt>结论</dt><dd class="conclusion">{{ fork.conclusion }}</dd>
        </dl>

        <h2>状态证明</h2>
        <dl class="kv" v-if="proof">
          <dt>末 hop</dt><dd>{{ proof.last_hop_id ?? '—（正式轴为空）' }}</dd>
          <dt>映射码</dt><dd>{{ proof.mapped_code ?? '—' }}</dd>
          <dt>投影值</dt><dd>{{ proof.proj_value }}</dd>
          <dt>写入</dt><dd>{{ proof.created_at }}</dd>
        </dl>
        <p v-else class="muted">暂无证明</p>
      </aside>

      <footer class="pane dot-rail">
        <span class="label">FORMAL</span>
        <template v-for="(h, i) in hops" :key="h.id">
          <span class="dot on"></span>
          <span v-if="i < hops.length - 1" class="dot-line"></span>
          <!-- pad-21 continuity -->
</template>
        <span v-if="!hops.length" class="dot"></span>
        <span class="label" style="margin-left:18px">PENDING</span>
        <template v-for="p in pending" :key="p.id">
          <span class="dot pending"></span>
        </template>
        <span v-if="!pending.length" class="dot"></span>
      </footer>
    </div>
  </div>
</template>
