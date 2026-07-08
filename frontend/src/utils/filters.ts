type DateLike = {
  format: (pattern: string) => string;
};

type DateRangeLike = [DateLike | null, DateLike | null] | null | undefined;

export function compactFilters<T extends object>(values: T) {
  return Object.fromEntries(
    Object.entries(values as Record<string, unknown>).filter(([, value]) => value !== undefined && value !== null && value !== ''),
  );
}

export function filtersWithDateRange(
  values: object,
  rangeKey: string,
  startKey: string,
  endKey: string,
) {
  const { [rangeKey]: rangeValue, ...rest } = values as Record<string, unknown>;
  const filters = compactFilters(rest);
  const range = rangeValue as DateRangeLike;

  if (range?.[0]) filters[startKey] = range[0].format('YYYY-MM-DD');
  if (range?.[1]) filters[endKey] = range[1].format('YYYY-MM-DD');

  return filters;
}
