export const formatTimestamp = (date = new Date()): string => (
  date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
);

export const formatRelativeTime = (date: Date, base = new Date()): string => {
  const diffMs = date.getTime() - base.getTime();
  const diffSeconds = Math.round(diffMs / 1000);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });

  const thresholds: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ['second', 60],
    ['minute', 60],
    ['hour', 24],
    ['day', 7],
    ['week', 4],
    ['month', 12],
    ['year', Number.POSITIVE_INFINITY],
  ];

  let remaining = diffSeconds;
  for (const [unit, limit] of thresholds) {
    if (Math.abs(remaining) < limit) {
      return rtf.format(remaining, unit);
    }
    remaining = Math.round(remaining / limit);
  }

  return rtf.format(0, 'second');
};
