<script setup>
import { onMounted, ref } from 'vue'
import { getAuthorizeUrlApi } from '@/api'

const failed = ref(false)

/** 进入登录页即自动跳转 CMS 授权登录,拉地址失败时展示重试按钮避免白屏 */
async function redirectToCms() {
  failed.value = false
  try {
    const res = await getAuthorizeUrlApi()
    window.location.assign(res.authorize_url)
  } catch (e) {
    // 错误提示已由 request 拦截器统一处理
    failed.value = true
  }
}

onMounted(redirectToCms)
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-card__title">创作引擎</div>
      <div class="login-card__subtitle">游戏库 · 素材 · 资产</div>
      <template v-if="!failed">
        <a-spin size="large" />
        <div class="login-card__tip">正在跳转 CMS 授权登录…</div>
      </template>
      <template v-else>
        <a-button type="primary" size="large" block @click="redirectToCms">
          重试登录
        </a-button>
        <div class="login-card__tip">跳转失败,请点击重试</div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: #090a0e;
}

.login-card {
  width: 360px;
  padding: 32px 24px;
  text-align: center;
  border-radius: 12px;
  background: linear-gradient(145deg, #252936, #171a23);
  border: 1px solid rgba(142, 159, 255, 0.12);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
}

.login-card__title {
  font-size: 22px;
  font-weight: 600;
  margin-bottom: 4px;
  color: #f4f5fa;
}

.login-card__subtitle {
  font-size: 13px;
  color: #8e9fff;
  margin-bottom: 24px;
}

.login-card__tip {
  margin-top: 16px;
  font-size: 12px;
  color: #737688;
}
</style>
