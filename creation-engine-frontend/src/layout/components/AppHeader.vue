<template>
  <header class="guest-header">
    <div class="wolf-logo" aria-label="代号：驰狼">
      <span class="wolf-letter">W</span>
      <span class="wolf-mark" aria-hidden="true">
        <img class="wolf-layer-base" src="/image/img_31.png" alt="" />
        <img class="wolf-layer-animal" src="/image/img_32.png" alt="" />
        <span class="wolf-layer-light">
          <i v-for="n in 9" :key="n" class="moon-particle"></i>
        </span>
      </span>
      <span class="wolf-letter">L</span>
      <span class="wolf-letter">F</span>
    </div>

  </header>
</template>
