import { createRouter, createWebHistory } from 'vue-router'
import Board from './pages/Board.vue'
import Ingest from './pages/Ingest.vue'
import AckCursor from './pages/AckCursor.vue'
import Quarantine from './pages/Quarantine.vue'
import Sidecar from './pages/Sidecar.vue'
import Proofs from './pages/Proofs.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Board },
    { path: '/ingest', component: Ingest },
    { path: '/ack', component: AckCursor },
    { path: '/sidecar', component: Sidecar },
    { path: '/quarantine', component: Quarantine },
    { path: '/proofs', component: Proofs },
  ],
})
