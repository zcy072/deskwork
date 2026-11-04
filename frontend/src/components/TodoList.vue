<script setup lang="ts">
import type { TodoItem } from '@/utils/steps'

defineProps<{ todos: TodoItem[] }>()
const icon = (s: string) => (s === 'completed' ? '✓' : s === 'in_progress' ? '●' : '○')
</script>

<template>
  <div v-if="todos.length" class="todos">
    <div class="head">任务计划({{ todos.filter((t) => t.status === 'completed').length }}/{{ todos.length }})</div>
    <div v-for="(t, i) in todos" :key="i" class="todo" :class="t.status">
      <span class="ic">{{ icon(t.status) }}</span>
      <span class="tx">{{ t.content }}</span>
    </div>
  </div>
</template>

<style scoped>
.todos { margin-bottom: 20px; padding: 12px 14px; background: var(--card-bg); border: 1px solid var(--line); border-radius: 12px; }
.head { font-weight: 600; margin-bottom: 8px; }
.todo { display: flex; gap: 8px; padding: 3px 0; font-size: 13px; }
.todo.completed { color: var(--muted); text-decoration: line-through; }
.todo.in_progress .ic { color: var(--accent); }
.ic { width: 14px; flex-shrink: 0; }
</style>
