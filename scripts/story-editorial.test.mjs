import test from 'node:test';
import assert from 'node:assert/strict';
import { applyStoryDates, storyInput, currentReviewedStories, selectEditedStories, editedStoryDocument } from '../story-editorial.mjs';
import { scienceView } from '../science.mjs';
const now = Date.parse('2026-10-14T16:00:00Z');
const raw = { id:'one', title:'One', summary:'Raw copy', url:'https://example.com/one', publishedAt:'2026-10-14T04:00:00Z' };
const story = { ...raw, summary:'Edited copy', publishedAt:'2026-10-13T16:00:00Z',
  editorial:{state:'verified', grounded:true, reviewedAt:'2026-10-14T14:00:00Z', expiresAt:'2026-10-17T14:00:00Z', inputFacts:storyInput(raw)} };
const review = { schemaVersion:'1.0.0',section:'science',generatedAt:'2026-10-14T14:00:00Z',stories:[story],
  days:[{date:'2026-10-14',selectedIds:['one']},{date:'2026-10-15',selectedIds:['one']}] };
test('changed source facts invalidate reviewed copy, while a corrected publication date survives',()=>{
  assert.deepEqual(currentReviewedStories([raw],[story]),[story]);
  assert.deepEqual(currentReviewedStories([{...raw,summary:'Changed'}],[story]),[]);
  const doc=editedStoryDocument({stories:[raw]},review,'science');
  assert.deepEqual(selectEditedStories(doc,'science','2026-10-14',now),[story]);
  assert.equal(doc.stories[0].publishedAt,'2026-10-13T16:00:00Z');
});
test('empty or unavailable reviews never promote unreviewed archive stories',()=>{
  const doc=editedStoryDocument({stories:[raw]}, {...review,stories:[]},'science');
  assert.deepEqual(selectEditedStories(doc,'science','2026-10-14',now),[]);
  assert.throws(()=>editedStoryDocument({stories:[raw]},null,'news'),/Missing/);
  assert.deepEqual(scienceView({...doc,stories:[raw]},'2026-10-14',now).stories,[]);
});
test('expiry, future dates and stale stories cannot become featured',()=>{
  const doc=editedStoryDocument({stories:[raw]},review,'science');
  for(const changed of [{...story,publishedAt:'2026-10-15T16:00:00Z'},
    {...story,editorial:{...story.editorial,grounded:false}},
    {...story,editorial:{...story.editorial,expiresAt:'2026-10-14T15:00:00Z'}},
    {...story,publishedAt:'2026-09-01T16:00:00Z'}]) {
    assert.deepEqual(selectEditedStories({...doc,stories:[changed]},'science','2026-10-14',now),[]);
  }
});
test('edited science keeps its daily order across four-hour rotation and retains archive access',()=>{
  const doc=editedStoryDocument({stories:[raw]},review,'science');
  const unreviewed={...raw,id:'archive'};
  const early=scienceView({...doc,stories:[story,unreviewed]},'2026-10-14',now);
  const later=scienceView({...doc,stories:[story,unreviewed]},'2026-10-14',now+4*3600000);
  assert.deepEqual(early.selectedIds,later.selectedIds);
  assert.deepEqual(early.archive,[unreviewed]);
  assert.deepEqual(selectEditedStories(doc,'science','2026-10-15',now),[story]);
});

test('source date corrections apply to archive stories only while inputs still match',()=>{
  const fixes=[{id:raw.id,inputFacts:storyInput(raw),publishedAt:'2026-10-07T17:16:00Z'}];
  assert.equal(applyStoryDates([raw],fixes)[0].publishedAt,'2026-10-07T17:16:00Z');
  assert.equal(applyStoryDates([{...raw,summary:'Changed'}],fixes)[0].publishedAt,raw.publishedAt);
});
