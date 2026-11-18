<script setup>
import { ref } from 'vue'
import { api } from '../api.js'

const result = ref(null)
const busy = ref(false)
const err = ref('')

async function run() {
  busy.value = true
  err.value = ''
  try {
    result.value = await api.ingest()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="page-card">
    <h1>装载</h1>
    <p class="lead">
      从本地夹具跑一批轨迹点装载。装载闸按每运单持久 ACK 分流：seq ≤ ACK 进正式有效轴，
      超 ACK / 无序号点进待 ACK 区；单事务失败闭，任一点剔除失败整趟回滚。
    </p>
    <button class="btn" :disabled="busy" @click="run">{{ busy ? '装载中…' : '跑装载批次' }}</button>
    <p v-if="err" class="muted" style="margin-top:12px">{{ err }}</p>
    <div v-if="result" style="margin-top:16px">
      <p :class="result.ok ? 'ack-ok' : 'ack-bad'">
        {{ result.ok ? '装载提交成功' : '装载失败（已整趟回滚）' }}
      </p>
      <table v-if="result.report" class="table" style="max-width:520px">
        <tbody>
          <tr><td>批次</td><td>{{ result.report.batch_id }}</td></tr>
          <tr><td>写入合计</td><td>{{ result.report.written }}</td></tr>
          <tr><td>进正式轴（≤ACK）</td><td class="ack-ok">{{ result.report.formal }}</td></tr>
          <tr><td>进待 ACK 区</td><td class="ack-warn">{{ result.report.pending_ack }}</td></tr>
          <tr><td>重复跳过</td><td>{{ result.report.duplicate }}</td></tr>
          <tr><td>输入点数</td><td>{{ result.report.input }}</td></tr>
        </tbody>
      </table>
      <pre v-if="!result.ok" style="font-family:var(--mono);font-size:12px;white-space:pre-wrap">{{ result.error }}</pre>
    </div>
  </div>
</template>
