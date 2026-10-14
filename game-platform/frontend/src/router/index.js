import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/login',
    name: 'Login',
    redirect: '/tasks',
    meta: { title: '任务日志' },
  },
  {
    path: '/auth/callback',
    name: 'AuthCallback',
    redirect: '/tasks',
    meta: { title: '任务日志' },
  },
  {
    path: '/',
    component: () => import('@/layout/index.vue'),
    redirect: '/tasks',
    children: [
      {
        path: 'tasks',
        name: 'Tasks',
        component: () => import('@/views/tasks/index.vue'),
        meta: { title: '任务日志' },
      },
      {
        path: 'usage',
        name: 'Usage',
        component: () => import('@/views/usage/index.vue'),
        meta: { title: 'Token用量' },
      },
      {
        // 老书签兜底,菜单已改为二级
        path: 'pipelines',
        redirect: '/pipelines/novel',
      },
      {
        path: 'pipelines/novel',
        name: 'PipelinesNovel',
        component: () => import('@/views/pipelines/index.vue'),
        meta: { title: '管线管理 · 视觉小说' },
      },
      {
        path: 'creation-test/novel',
        name: 'CreationTestNovel',
        component: () => import('@/views/creationTest/novel.vue'),
        meta: { title: '创作测试 · 视觉小说' },
      },
      {
        path: 'assets/:category',
        name: 'Assets',
        component: () => import('@/views/assets/index.vue'),
        meta: { title: '素材库' },
      },
      {
        path: 'games/novel',
        name: 'GamesNovel',
        component: () => import('@/views/games/novel/index.vue'),
        meta: { title: '视觉小说' },
      },
      {
        path: 'games/promo',
        name: 'GamesPromo',
        component: () => import('@/views/games/promo/index.vue'),
        meta: { title: '创作推广' },
      },
    ],
  },
  {
    path: '/404',
    name: 'NotFound',
    component: () => import('@/views/404/index.vue'),
    meta: { title: '页面不存在' },
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/404',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

/** 本地模式无需登录；保留路由钩子只负责设置页面标题。 */
router.beforeEach((to, _from, next) => {
  document.title = to.meta.title ? `${to.meta.title} - Agent中台` : 'Agent中台'
  next()
})

export default router
