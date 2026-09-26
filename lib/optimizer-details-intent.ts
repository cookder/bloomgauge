export const OPTIMIZER_DETAILS_EVENT = 'bloom:optimizer-details';
let requested = false;
export function requestOptimizerDetails() {
  requested = true;
  window.dispatchEvent(new Event(OPTIMIZER_DETAILS_EVENT));
}
export function consumeOptimizerDetails() {
  const value = requested;
  requested = false;
  return value;
}
