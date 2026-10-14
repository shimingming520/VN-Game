<script setup>
import AppHeader from './components/AppHeader.vue'
import AppSider from './components/AppSider.vue'
</script>

<template>
  <a-layout class="app-layout">
    <AppHeader />
    <a-layout>
      <AppSider />
      <a-layout-content class="app-layout__content">
        <!-- 四角回字纹角标 + 龙鳞水印 + 狐火,主题元素落在每个业务页背后 -->
        <i class="dr-corner dr-corner--tl"></i>
        <i class="dr-corner dr-corner--tr"></i>
        <i class="dr-corner dr-corner--bl"></i>
        <i class="dr-corner dr-corner--br"></i>
        <span class="app-layout__seal"></span>
        <div class="fox-fire" style="top: 12%; right: 8%; left: auto; animation-delay: 0.6s"></div>
        <div class="fox-fire fox-fire--gold" style="bottom: 14%; left: 5%; animation-delay: 2.3s"></div>

        <div class="app-layout__inner">
          <router-view />
        </div>
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>

<style scoped>
.app-layout {
  min-height: 100vh;
}

.app-layout {
  background: transparent !important;
}

.app-layout__content {
  position: relative;
  margin: 16px;
  padding: 24px;
  background: var(--dr-grad-panel);
  border: 1px solid var(--dr-border);
  border-radius: 14px;
  min-height: calc(100vh - 64px - 32px);
  box-shadow: 0 18px 44px rgba(0, 0, 0, 0.35);
  overflow: hidden;
}
/* 面板顶边金青细线,呼应顶栏流光 */
.app-layout__content::before {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(232, 182, 76, 0.55), rgba(53, 214, 208, 0.45), transparent);
  z-index: 2;
}

/* 龙鳞纹在面板内浅浅铺一层,让内页不只是"换了底色" */
.app-layout__content::after {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  background-image:
    radial-gradient(circle at 50% 100%, rgba(232, 182, 76, 0.045) 0 13px, transparent 14px),
    radial-gradient(circle at 50% 100%, rgba(53, 214, 208, 0.035) 0 13px, transparent 14px);
  background-size: 38px 24px, 38px 24px;
  background-position: 0 0, 19px 12px;
}

/* 右下角龙纹印记水印 */
.app-layout__seal {
  position: absolute;
  right: 26px;
  bottom: 20px;
  width: 190px;
  height: 190px;
  pointer-events: none;
  background: url('/dragon.svg') center / contain no-repeat;
  opacity: 0.045;
  filter: hue-rotate(285deg) saturate(1.4) brightness(1.7);
}

/* 业务内容压在纹理之上 */
.app-layout__inner {
  position: relative;
  z-index: 1;
}
</style>
