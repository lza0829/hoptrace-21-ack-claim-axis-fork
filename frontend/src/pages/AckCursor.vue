<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const proofs = ref([])
const pending = ref([])
const audits = ref([])
const receipts = ref([])
const demo = ref(null)
const tearDemo = ref(null)
const err = ref('')
const loading = ref(false)

async function loadAll() {
  loading.value = true
  err.value = ''
  try {
    const [p, q, a, r] = await Promise.all([
      api.forkProofs(), api.pendingHops(), api.tearAudits(), api.promotionProofs(),
    ])
    proofs.value = p
    pending.value = q
    audits.value = a
    receipts.value = r
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

async function runTearDemo() {
  err.value = ''
  try {
    tearDemo.value = await api.promotionTearDemo()
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
      ACK 下调整笔拒绝。晋升成功必须让待确认点进入正式有效轴并同事务重算投影，失败则待确认与投影
      保持晋升前。本页只读证明 / 晋升回执 / 撕裂核证 + 两组对照演示，推进入口在测绘板运单检查器。
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

    <h2 class="block-title">晋升撕裂核证（只读）</h2>
    <p class="muted small">
      逐条对账晋升回执与正式轴 / 待确认 / 投影：回执晋升点必须可查、正式轴点数必须增、
      待确认不得被无回执清空、正式轴与回执一致时投影不得停在晋升前。
    </p>
    <table class="table">
      <thead>
        <tr><th>运单</th><th>ACK</th><th>正式点数</th><th>待确认键</th><th>投影</th><th>回执数</th><th>核证结论</th></tr>
      </thead>
      <tbody>
        <tr v-for="a in audits" :key="a.waybill_id">
          <td>{{ a.waybill_id }}</td>
          <td>{{ a.ack_seq }}</td>
          <td>{{ a.formal_count }}</td>
          <td>{{ a.pending_keys.join(', ') || '∅' }}</td>
          <td>{{ a.projection }}</td>
          <td>{{ a.promotion_receipts.length }}</td>
          <td :class="a.tear ? 'ack-bad' : 'ack-ok'">{{ a.verdict }}</td>
        </tr>
        <tr v-if="!audits.length && !loading"><td colspan="7" class="muted">暂无核证行</td></tr>
      </tbody>
    </table>

    <h2 class="block-title">晋升回执（独立晋升对照 / 撕裂证明表）</h2>
    <table class="table">
      <thead>
        <tr>
          <th>回执</th><th>运单</th><th>ACK 前→后</th><th>晋升 id 列表</th>
          <th>正式点数 前→后</th><th>投影 前→后</th><th>晋升后待确认</th><th>时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in receipts" :key="r.promo_id">
          <td>{{ r.promo_id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.previous_ack_seq }} → {{ r.ack_seq }}</td>
          <td>{{ r.promoted_ids.join(', ') || '∅' }}</td>
          <td :class="r.formal_count_after > r.formal_count_before ? 'ack-ok' : 'ack-bad'">
            {{ r.formal_count_before }} → {{ r.formal_count_after }}
          </td>
          <td>{{ r.projection_before }} → {{ r.projection_after }}</td>
          <td>{{ r.pending_after.join(', ') || '∅' }}</td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!receipts.length && !loading"><td colspan="8" class="muted">暂无晋升回执</td></tr>
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

    <h2 class="block-title">晋升撕裂对照：先装超游标点再上调游标（成功 / 失败）</h2>
    <p class="muted small">
      夹具 <span class="mono">{{ tearDemo?.fixture || 'promotion_tear.json' }}</span>：
      ACK=0 时先装载全部超游标点（全落待确认区），再上调 ACK。
      成功路径要求晋升点入正式轴、投影同事务前进、写晋升回执；
      失败路径在事务中途注入撕裂，整趟回滚，待确认与投影保持晋升前。
    </p>
    <button class="btn" @click="runTearDemo">跑晋升撕裂对照</button>

    <div v-if="tearDemo" class="demo-grid">
      <div class="demo-col">
        <h3>{{ tearDemo.success.name }}</h3>
        <div class="muted mono small">装载结果：</div>
        <div class="mono small">
          <span v-for="v in tearDemo.success.load.load_verdicts" :key="v.hop_id" class="demo-pill">
            {{ v.hop_id }}(seq {{ v.seq }}) → {{ v.verdict }}
          </span>
        </div>
        <div class="demo-step">
          <div class="demo-step-head">晋升前</div>
          <div class="muted mono small">
            ACK={{ tearDemo.success.before.ack_seq }} ·
            正式={{ tearDemo.success.before.formal_ids.length }} ·
            待确认=[{{ tearDemo.success.before.pending_ids.join(', ') }}] ·
            投影={{ tearDemo.success.before.projection }}
          </div>
        </div>
        <div class="demo-step">
          <div class="demo-step-head">晋升回执 · {{ tearDemo.success.receipt.promotion_proof_id }}</div>
          <div class="muted mono small">
            晋升 id=[{{ tearDemo.success.receipt.promoted.join(', ') }}] ·
            正式点数={{ tearDemo.success.receipt.formal_count }} ·
            投影={{ tearDemo.success.receipt.projection }}
          </div>
        </div>
        <div class="demo-step">
          <div class="demo-step-head">晋升后</div>
          <div class="muted mono small">
            ACK={{ tearDemo.success.after.ack_seq }} ·
            正式=[{{ tearDemo.success.after.formal_ids.join(', ') }}] ·
            待确认=[{{ tearDemo.success.after.pending_ids.join(', ') }}] ·
            投影={{ tearDemo.success.after.projection }} ·
            回执={{ tearDemo.success.after.promotion_receipts }}
          </div>
        </div>
        <div :class="tearDemo.success.audit.tear ? 'ack-bad' : 'ack-ok'">
          核证：{{ tearDemo.success.audit.verdict }}
        </div>
      </div>

      <div class="demo-col">
        <h3>失败对照（整趟回滚）</h3>
        <div v-for="f in tearDemo.failures" :key="f.name" class="demo-step">
          <div class="demo-step-head">{{ f.name }}</div>
          <div class="ack-bad small">{{ f.error }}</div>
          <div class="muted mono small">
            回滚={{ f.rolled_back ? '是' : '否' }} ·
            待确认保持=[{{ f.after.pending_ids.join(', ') }}] ·
            投影保持={{ f.after.projection }} ·
            正式点数={{ f.after.formal_ids.length }} ·
            回执={{ f.after.promotion_receipts }}
          </div>
        </div>
        <div class="muted small">{{ tearDemo.verdict.failure }}</div>
      </div>
    </div>
  </div>
</template>
