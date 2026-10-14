<script setup>
import AppHeader from './components/AppHeader.vue'
import AppSider from './components/AppSider.vue'
</script>

<template>
  <div class="guest-app">
    <AppHeader />
    <AppSider />
    <main>
      <router-view />
    </main>
  </div>
</template>
