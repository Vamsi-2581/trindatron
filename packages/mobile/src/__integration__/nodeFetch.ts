// Node's fetch already streams response bodies, which is what expo/fetch provides on devices.
export const fetch = globalThis.fetch
