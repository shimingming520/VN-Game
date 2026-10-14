<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { loginApi } from '@/api'
import { consumeAuthReturn, setAuth } from '@/store/auth'

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
    router.replace(consumeAuthReturn())
  } catch (e) {
    // 错误提示已由 request 拦截器统一处理
    error.value = '登录失败，请重新发起登录'
  }
})
</script>

<template>
  <div class="callback-page">
    <div class="callback-card">
      <template v-if="!error">
        <a-spin size="large" />
        <div class="callback-card__text">正在完成登录…</div>
      </template>
      <template v-else>
        <div class="callback-card__error">{{ error }}</div>
        <a-button type="primary" @click="router.replace('/login')">返回登录页</a-button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.callback-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: #090a0e;
}

.callback-card {
  width: 360px;
  padding: 24px 16px;
  text-align: center;
  border-radius: 12px;
  background: linear-gradient(145deg, #252936, #171a23);
  border: 1px solid rgba(142, 159, 255, 0.12);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
}

.callback-card__text {
  margin-top: 16px;
  color: #8e9fff;
}

.callback-card__error {
  margin-bottom: 16px;
  color: #ff4d4f;
}
</style>
