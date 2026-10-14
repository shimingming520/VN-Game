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
        <router-view />
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>

<style scoped>
.app-layout {
  min-height: 100vh;
}

.app-layout__content {
  margin: 16px;
  padding: 24px;
  background: #fff;
  border-radius: 8px;
  min-height: calc(100vh - 64px - 32px);
}
</style>
