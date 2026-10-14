import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    component: () => import('@/layout/index.vue'),
    redirect: '/home',
    children: [
      {
        path: 'home',
        name: 'Home',
        component: () => import('@/views/home/index.vue'),
        meta: { title: '首页' },
      },
      {
        path: 'library',
        name: 'Library',
        component: () => import('@/views/library/index.vue'),
        meta: { title: '游戏仓库' },
      },
      {
        path: 'create',
        name: 'Create',
        component: () => import('@/views/create/index.vue'),
        meta: { title: '创作' },
      },
      {
        path: 'projects',
        name: 'Projects',
        component: () => import('@/views/projects/index.vue'),
        meta: { title: '全部项目' },
      },
      {
        path: 'assets/:category',
        name: 'Assets',
        component: () => import('@/views/assets/index.vue'),
        meta: { title: '素材' },
      },
      {
        path: 'my-assets',
        name: 'MyAssets',
        component: () => import('@/views/myAssets/index.vue'),
        meta: { title: '资产' },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/home',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

/** 路由前置守卫：仅设置页面标题，默认允许直接进入生产流程。 */
router.beforeEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} - 创作引擎` : '创作引擎'
  return true
})

export default router
