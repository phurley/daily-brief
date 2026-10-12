import { fitOrder, saveWeights, compareTaste, WEIGHTS_KEY } from './weight-tuning.mjs';
import { hasSignals } from './scoring.mjs?v=20261009-2';

const label = name => name.replaceAll('_', ' ');
function element(tag, attributes = {}, text = '') {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  node.textContent = text;
  return node;
}

export function calendarTuner({ host, getWeights, getDefaults, setWeights, getEvents, refresh, storage }) {
  const panel = element('details', { class: 'calendar-tuning' });
  panel.append(element('summary', {}, 'Adjust event ranking'));
  const description = element('p', {}, 'Positive weights favor a signal; negative weights penalize it. Changes save in this browser and update best bets too. Drag an event onto another, or use ↑/↓, to request a rank. The fitter attempts that order; some orders are impossible.');
  const toolbar = element('div', { class: 'calendar-tuning__toolbar' });
  const sortLabel = element('label', {}, 'Agenda order ');
  const sort = element('select', { 'aria-label': 'Agenda order' });
  sort.append(element('option', { value: 'rating' }, 'Taste rating'), element('option', { value: 'time' }, 'Start time'));
  sortLabel.append(sort);
  const undo = element('button', { type: 'button', disabled: '' }, 'Undo weight change');
  const reset = element('button', { type: 'button' }, 'Reset to defaults');
  toolbar.append(sortLabel, undo, reset);
  const status = element('p', { class: 'calendar-tuning__status', role: 'status' }, 'Weights save on this browser. Equal ratings use the unrounded score to sort.');
  const controls = element('div', { class: 'calendar-tuning__weights' });
  panel.append(description, controls);
  host.append(toolbar, status, panel);
  let history = [], dragged = null, pending = false;
  const remember = () => {
    history.push(structuredClone(getWeights()));
    if (history.length > 30) history.shift();
    undo.disabled = false;
  };
  const persist = message => {
    const saved = saveWeights(getWeights(), storage);
    status.textContent = `${message} ${saved ? 'Saved on this browser.' : 'Browser storage unavailable; changes last only for this visit.'}`;
  };
  const update = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; refresh(); });
  };
  function render() {
    controls.replaceChildren();
    const config = getWeights();
    if (!config?.signals) {
      controls.append(element('p', {}, 'Weights unavailable. Refresh the brief to retry.'));
      reset.disabled = true;
      return;
    }
    reset.disabled = false;
    for (const [name, spec] of Object.entries(config.signals)) {
      const row = element('label', { class: 'calendar-tuning__weight' });
      const title = element('span', {}, label(name));
      const signed = spec.weight * (spec.polarity === 'negative' ? -1 : 1);
      const slider = element('input', { type: 'range', min: '-1', max: '1', step: '0.01', value: String(signed), 'aria-label': `${label(name)} weight slider` });
      const number = element('input', { type: 'number', min: '-1', max: '1', step: '0.01', value: String(Number(signed.toFixed(4))), 'aria-label': `${label(name)} weight` });
      slider.addEventListener('pointerdown', remember);
      slider.addEventListener('keydown', e => { if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(e.key)) remember(); });
      number.addEventListener('focus', remember);
      const change = (source, other) => {
        if (source.value === '' || !Number.isFinite(source.valueAsNumber)) return;
        const value = Math.max(-1, Math.min(1, source.valueAsNumber));
        const weights = structuredClone(getWeights());
        weights.signals[name] = { ...weights.signals[name], weight: Math.abs(value), polarity: value < 0 ? 'negative' : 'positive' };
        setWeights(weights);
        other.value = String(value);
        sort.value = 'rating';
        persist('Ranking updated.');
        update();
      };
      slider.addEventListener('input', () => change(slider, number));
      number.addEventListener('input', () => change(number, slider));
      row.append(title, slider, number);
      controls.append(row);
    }
  }
  function move(id, targetId) {
    if (sort.value !== 'rating') return;
    const events = getEvents();
    const from = events.findIndex(e => e.id === id), to = events.findIndex(e => e.id === targetId);
    if (from < 0 || to < 0 || from === to) return;
    if (events.some(e => !hasSignals(e))) {
      status.textContent = 'Cannot fit this day: some events have no scoring signals. You can still adjust the weights manually.';
      return;
    }
    const desired = [...events];
    desired.splice(to, 0, desired.splice(from, 1)[0]);
    remember();
    const result = fitOrder(desired, getWeights(), { movedId: id });
    setWeights(result.config);
    const actualRank = [...desired].sort((a, b) => compareTaste(a, b, result.config)).findIndex(event => event.id === id) + 1;
    persist(`${result.matched === result.total ? 'Requested order matched.' : `Best attempt: ${result.matched} of ${result.total} requested event pairs matched (${result.ties} tied).`} Moved event is #${actualRank} (requested #${to + 1}). Showing the fitted ranking. Undo restores the previous weights.`);
    render();
    refresh();
    host.closest('dialog')?.querySelector(`[data-move-id="${CSS.escape(String(id))}"]:not(:disabled)`)?.focus({ preventScroll: true });
  }
  function decorate(button, event, events) {
    const wrapper = element('div', { class: 'calendar-tuning__event' });
    wrapper.append(button);
    if (sort.value !== 'rating' || !getWeights()?.signals || !hasSignals(event)) return wrapper;
    const index = events.findIndex(e => e.id === event.id);
    const moves = element('div', { class: 'calendar-tuning__moves' });
    for (const [offset, text] of [[-1, '↑'], [1, '↓']]) {
      const control = element('button', { type: 'button', 'aria-label': `Move ${event.title} ${offset < 0 ? 'up' : 'down'}`, 'data-move-id': String(event.id) }, text);
      control.disabled = !events[index + offset];
      control.addEventListener('click', () => move(event.id, events[index + offset]?.id));
      moves.append(control);
    }
    wrapper.append(moves);
    button.draggable = true;
    button.title = 'Drag to request a new rank';
    button.addEventListener('dragstart', e => { dragged = event.id; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(event.id)); });
    button.addEventListener('dragend', () => { dragged = null; document.querySelectorAll('.is-drop-target').forEach(n => n.classList.remove('is-drop-target')); });
    wrapper.addEventListener('dragover', e => { if (dragged !== null) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; wrapper.classList.add('is-drop-target'); } });
    wrapper.addEventListener('dragleave', e => { if (!wrapper.contains(e.relatedTarget)) wrapper.classList.remove('is-drop-target'); });
    wrapper.addEventListener('drop', e => { e.preventDefault(); wrapper.classList.remove('is-drop-target'); if (dragged !== null) move(dragged, event.id); dragged = null; });
    return wrapper;
  }
  sort.addEventListener('change', refresh);
  undo.addEventListener('click', () => {
    if (!history.length) return;
    setWeights(history.pop()); undo.disabled = !history.length;
    persist('Previous weights restored.'); render(); refresh();
  });
  reset.addEventListener('click', () => {
    remember(); setWeights(structuredClone(getDefaults()));
    persist('Default weights restored.'); render(); refresh();
  });
  window.addEventListener('storage', e => {
    if (e.key !== WEIGHTS_KEY && e.key !== null) return;
    // Ranking is reloaded by the host so all score surfaces use one config.
    history = []; undo.disabled = true;
    host.dispatchEvent(new Event('weights-storage-change'));
    render(); refresh(); status.textContent = 'Weights updated from another tab.';
  });
  render();
  return { decorate, render, byRating: () => sort.value === 'rating' };
}
