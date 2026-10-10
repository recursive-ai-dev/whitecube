export const LOG_HISTORY_LIMIT = 250;

export const CATEGORY_LABELS = {
  System: 'System',
  Files: 'Files',
  Network: 'Network',
  Development: 'Development',
  Custom: 'Custom',
} as const;

export const STATUS_LABELS = {
  IDLE: 'Idle',
  RUNNING: 'Running',
  SUCCESS: 'Success',
  ERROR: 'Failed',
} as const;
