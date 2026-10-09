// Cancellation notices remain visible in the calendar, but are not recommendations.
export function isEventRecommended(event) {
  return !["canceled", "postponed"].includes(event?.status);
}
export function eventDisplayTitle(event) {
  const labels = { canceled: "Canceled", postponed: "Postponed", rescheduled: "Rescheduled" };
  const label = labels[event?.status];
  return label && !/^(cancell?ed|postponed|rescheduled)\b/i.test(event.title || "")
    ? `${label}: ${event.title}` : event.title;
}
