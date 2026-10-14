<script setup>
import ScriptComposer from '@/components/ScriptComposer.vue'

defineProps({
  sourceTaskId: { type: String, default: '' },
})

const emit = defineEmits(['uploaded', 'submitted-batch'])

function handleUploaded(payload) {
  emit('uploaded', payload)
}

function handleBatchSubmitted() {
  emit('submitted-batch')
}
</script>

<template>
  <ScriptComposer
    :show-sample="true"
    :source-task-id="sourceTaskId"
    @uploaded="handleUploaded"
    @submitted-batch="handleBatchSubmitted"
  />
</template>
