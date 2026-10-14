<script setup>
import { useRouter } from 'vue-router'

const router = useRouter()

function goHome() {
  router.push('/tasks')
}
</script>

<template>
  <div class="not-found-page dr-cloud">
    <div class="fox-fire" style="top: 28%; left: 32%; animation-delay: 0s"></div>
    <div class="fox-fire fox-fire--gold" style="top: 62%; left: 66%; animation-delay: 1.2s"></div>

    <div class="not-found-page__inner">
      <span class="not-found-page__crest">
        <img src="/fox.svg" alt="" />
      </span>
      <div class="not-found-page__code dr-title">404</div>
      <div class="not-found-page__text">此路无痕 · 你访问的页面不存在</div>
      <a-button type="primary" @click="goHome">返回首页</a-button>
    </div>
  </div>
</template>

<style scoped>
.not-found-page {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  overflow: hidden;
  background:
    radial-gradient(700px 500px at 30% 25%, rgba(212, 35, 122, 0.14), transparent 65%),
    radial-gradient(700px 500px at 75% 78%, rgba(53, 214, 208, 0.12), transparent 65%),
    linear-gradient(160deg, #0c111b 0%, #070a11 100%);
}

.not-found-page__inner {
  position: relative;
  z-index: 1;
  text-align: center;
}

.not-found-page__crest {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 72px;
  height: 72px;
  margin-bottom: 18px;
  border-radius: 22px;
  background: radial-gradient(circle at 30% 25%, #2b3552, #141a27);
  box-shadow: var(--dr-glow-cyan);
}
.not-found-page__crest img {
  width: 40px;
  height: 40px;
  filter: drop-shadow(0 0 10px rgba(53, 214, 208, 0.65));
}

.not-found-page__code {
  background: var(--dr-grad-gold);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  font-size: 68px;
  letter-spacing: 8px;
  line-height: 1;
}

.not-found-page__text {
  margin: 12px 0 24px;
  color: var(--dr-text-3);
  font-size: 13px;
  letter-spacing: 2px;
}
</style>
