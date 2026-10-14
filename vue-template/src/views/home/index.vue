<template>
  <div class="home-page">
    <a-typography-title :level="3">欢迎使用 Vue Template</a-typography-title>
    <a-typography-paragraph type="secondary">
      基于 Vite + Vue 3 + Ant Design Vue + Axios + Vue Router
      搭建的前端基础框架。
    </a-typography-paragraph>

    <a-row :gutter="[16, 16]" class="home-page__grid">
      <a-col
        v-for="item in features"
        :key="item.title"
        :xs="24"
        :sm="12"
        :md="8"
      >
        <a-card hoverable>
          <a-typography-title :level="5">{{ item.title }}</a-typography-title>
          <a-typography-text type="secondary">{{
            item.desc
          }}</a-typography-text>
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>
<script setup>
/** 首页展示数据 */
const features = [
  { title: "Vite", desc: "极速开发与构建" },
  { title: "Vue 3", desc: "组合式 API + script setup" },
  { title: "Ant Design Vue", desc: "企业级 UI 组件库" },
  { title: "Vue Router", desc: "单页应用路由管理" },
  { title: "Axios", desc: "HTTP 请求封装与拦截" },
];
</script>

<style scoped>
.home-page__grid {
  margin-top: 24px;
}
</style>
