<script setup>
import { computed } from 'vue'
import { authState } from '@/store/auth'

const username = computed(() => authState.user?.username || '本地用户')
const initial = computed(() => (username.value || '本').slice(0, 1).toUpperCase())
</script>

<template>
  <a-layout-header class="app-header">
    <div class="app-header__brand">
      <span class="app-header__crest">
        <img src="/dragon.svg" alt="" />
      </span>
      <span class="app-header__titles">
        <b class="app-header__name dr-title">AGENT 中台</b>
        <span class="app-header__sub">CONTENT&nbsp;·&nbsp;PIPELINE&nbsp;·&nbsp;ORCHESTRATION</span>
      </span>
    </div>

    <div class="app-header__user">
      <span class="app-header__badge">本地模式</span>
      <span class="app-header__avatar">{{ initial }}</span>
      <span class="app-header__username">{{ username }}</span>
    </div>
  </a-layout-header>
</template>

<style scoped>
.app-header {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 64px;
  padding: 0 24px;
  background: linear-gradient(90deg, #0b1018 0%, #131a28 45%, #0c111a 100%);
  border-bottom: 1px solid var(--dr-border);
}
/* 底部一道金-玫-青流光,视觉亮点 */
.app-header::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 2px;
  background: linear-gradient(90deg, transparent, #e8b64c 18%, #d4237a 50%, #35d6d0 82%, transparent);
  opacity: 0.85;
}

.app-header__brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.app-header__crest {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 38px;
  border-radius: 11px;
  background: radial-gradient(circle at 30% 25%, #2a3450, #141a27);
  box-shadow: var(--dr-glow-gold);
}
.app-header__crest img {
  width: 24px;
  height: 24px;
  /* 图标原色偏玫红,染成龙金 */
  filter: drop-shadow(0 0 6px rgba(232, 182, 76, 0.55)) hue-rotate(285deg) saturate(1.35)
    brightness(1.25);
}
.app-header__titles {
  display: flex;
  flex-direction: column;
  line-height: 1.15;
}
.app-header__name {
  background: var(--dr-grad-gold);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  font-size: 19px;
  font-weight: 700;
  letter-spacing: 3px;
}
.app-header__sub {
  color: var(--dr-text-3);
  font-size: 9px;
  letter-spacing: 2px;
}

.app-header__user {
  display: flex;
  align-items: center;
  gap: 10px;
}
.app-header__badge {
  padding: 3px 9px;
  border: 1px solid rgba(232, 182, 76, 0.4);
  border-radius: 999px;
  color: var(--dr-gold-2);
  font-size: 11px;
}
.app-header__avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  background: var(--dr-grad-cyan);
  color: #06231f;
  font-size: 13px;
  font-weight: 700;
}
.app-header__username {
  color: var(--dr-text);
  font-size: 13px;
}
.app-header__logout {
  padding: 5px 14px;
  border: 1px solid var(--dr-border);
  border-radius: 8px;
  background: transparent;
  color: var(--dr-text-2);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.18s ease;
}
.app-header__logout:hover {
  border-color: var(--dr-magenta);
  color: #ff8ec0;
}
</style>
