<script setup>
import { onMounted, ref } from 'vue'
import { getAuthorizeUrlApi } from '@/api'

const failed = ref(false)

/**
 * 进入登录页即自动跳转 CMS 授权登录:后端生成 state+PKCE 并返回 CMS OAuth2 登录页地址,
 * 整页跳转;管理员登录授权后浏览器带 code&state 回 /auth/callback。
 * 拉地址失败(如后端不可用)时不整页跳,展示重试按钮避免白屏。
 */
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
  <div class="login-page dr-cloud">
    <div class="login-page__bg"></div>
    <!-- 狐火/龙焰粒子 -->
    <div class="fox-fire" style="top: 22%; left: 26%; animation-delay: 0s"></div>
    <div class="fox-fire" style="top: 66%; left: 72%; animation-delay: 1.5s"></div>
    <div class="fox-fire fox-fire--gold" style="top: 38%; left: 80%; animation-delay: 0.8s"></div>
    <div class="fox-fire fox-fire--gold" style="top: 78%; left: 18%; animation-delay: 2.1s"></div>

    <a-card class="login-card" :bordered="false">
      <div class="login-card__crests">
        <span class="login-card__crest login-card__crest--fox">
          <img src="/fox.svg" alt="" />
        </span>
        <span class="login-card__crest login-card__crest--dragon">
          <img src="/dragon.svg" alt="" />
        </span>
      </div>
      <div class="login-card__title dr-title">龙族</div>
      <div class="login-card__subtitle">AGENT 中台 · 跨模块创作与调度</div>
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
    </a-card>
  </div>
</template>

<style scoped>
.login-page {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  overflow: hidden;
  background:
    radial-gradient(900px 600px at 15% 20%, rgba(212, 35, 122, 0.16), transparent 65%),
    radial-gradient(800px 600px at 85% 80%, rgba(53, 214, 208, 0.14), transparent 65%),
    linear-gradient(160deg, #0c111b 0%, #070a11 100%);
}
/* 主视觉背景图(暗化压底,不抢卡片) */
.login-page__bg {
  position: absolute;
  inset: 0;
  background: url('/20260727-142825.jpeg') center / cover no-repeat;
  opacity: 0.3;
  filter: saturate(0.95) contrast(1.05);
  pointer-events: none;
}
/* 背景灵纹光环(缓慢旋转) */
.login-page::before {
  content: '';
  position: absolute;
  width: 780px;
  height: 780px;
  border-radius: 50%;
  border: 1px solid rgba(232, 182, 76, 0.12);
  box-shadow: inset 0 0 120px rgba(232, 182, 76, 0.06);
  animation: dr-spin 46s linear infinite;
}
@keyframes dr-spin {
  to {
    transform: rotate(360deg);
  }
}

.login-card {
  position: relative;
  z-index: 1;
  width: 380px;
  padding: 24px 20px;
  text-align: center;
  border: 1px solid var(--dr-border) !important;
  border-radius: 16px;
  background: var(--dr-grad-panel) !important;
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55), var(--dr-glow-gold);
}

.login-card__crests {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  margin-bottom: 14px;
}
.login-card__crest {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 62px;
  height: 62px;
  border-radius: 20px;
  background: radial-gradient(circle at 30% 25%, #2b3552, #141a27);
}
.login-card__crest img {
  width: 36px;
  height: 36px;
}
/* 龙:金色发光 */
.login-card__crest--dragon {
  box-shadow: var(--dr-glow-gold);
}
.login-card__crest--dragon img {
  filter: drop-shadow(0 0 10px rgba(232, 182, 76, 0.6)) hue-rotate(285deg) saturate(1.35)
    brightness(1.25);
}
/* 狐:青色狐火发光,与龙形成双生对照 */
.login-card__crest--fox {
  color: var(--dr-cyan-2);
  box-shadow: var(--dr-glow-cyan);
}
.login-card__crest--fox img {
  filter: drop-shadow(0 0 10px rgba(53, 214, 208, 0.65));
}

.login-card__title {
  margin-bottom: 6px;
  background: var(--dr-grad-gold);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: 5px;
}

.login-card__subtitle {
  margin-bottom: 26px;
  color: var(--dr-text-3);
  font-size: 12px;
  letter-spacing: 1.5px;
}

.login-card__tip {
  margin-top: 16px;
  color: var(--dr-text-3);
  font-size: 12px;
}
</style>
