<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { loginApi } from '@/api'
import { setAuth } from '@/store/auth'

const route = useRoute()
const router = useRouter()
const error = ref('')

onMounted(async () => {
  const { code, state } = route.query
  if (!code || !state) {
    error.value = '回调参数缺失，请重新登录'
    return
  }
  try {
    const res = await loginApi(code, state)
    setAuth(res.token, res.user)
    message.success(`欢迎，${res.user.username}`)
    router.replace('/tasks')
  } catch (e) {
    // 错误提示已由 request 拦截器统一处理
    error.value = '登录失败，请重新发起登录'
  }
})
</script>

<template>
  <div class="callback-page dr-cloud">
    <div class="fox-fire" style="top: 30%; left: 38%; animation-delay: 0.4s"></div>
    <div class="fox-fire fox-fire--gold" style="top: 66%; left: 60%; animation-delay: 1.6s"></div>
    <a-card class="callback-card" :bordered="false">
      <template v-if="!error">
        <a-spin size="large" />
        <div class="callback-card__text">正在完成登录…</div>
      </template>
      <template v-else>
        <div class="callback-card__error">{{ error }}</div>
        <a-button type="primary" @click="router.replace('/login')">返回登录页</a-button>
      </template>
    </a-card>
  </div>
</template>

<style scoped>
.callback-page {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  overflow: hidden;
  background:
    radial-gradient(800px 560px at 20% 22%, rgba(212, 35, 122, 0.14), transparent 65%),
    radial-gradient(760px 520px at 82% 78%, rgba(53, 214, 208, 0.12), transparent 65%),
    linear-gradient(160deg, #0c111b 0%, #070a11 100%);
}

.callback-card {
  position: relative;
  z-index: 1;
  width: 360px;
  padding: 24px 16px;
  text-align: center;
  border: 1px solid var(--dr-border) !important;
  border-radius: 16px;
  background: var(--dr-grad-panel) !important;
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5), var(--dr-glow-gold);
}

.callback-card__text {
  margin-top: 16px;
  color: var(--dr-text-2);
  letter-spacing: 1px;
}

.callback-card__error {
  margin-bottom: 16px;
  color: #ff4d4f;
}
</style>
