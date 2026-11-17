const {test}=require('node:test');
const assert=require('node:assert/strict');
const {reviewSummary,summaryNoteState,transcriptReference}=require('../summary-review.js');
const ref={keywords:[{label:'resigned',terms:['resigned','resignation','사임','사퇴']},{label:'relationship',terms:['relationship','affair','관계','불륜']} ]};
test('same broadcast retains note, different broadcast clears it',()=>{
 const old={episodeId:'first',text:'Saved summary'};
 assert.deepEqual(summaryNoteState(old,'first'),old);
 assert.deepEqual(summaryNoteState(old,'next'),{episodeId:'next',text:''});
 assert.deepEqual(summaryNoteState(null,'next'),{episodeId:'next',text:''});
});
test('English and Korean aliases both receive content feedback',()=>{
 for(const text of ['The official resigned from his position. He had a relationship with an executive.','해당 임원이 사임했다고 보도했습니다. 다른 임원과의 관계가 문제가 되었습니다.']){
  const r=reviewSummary(text,ref);assert.equal(r.score,100);assert.equal(r.missing.length,0);
 }
});
test('irrelevant summary does not get content points; missing concepts shown',()=>{
 const r=reviewSummary('I enjoyed the music. The weather was nice today.',ref);
 assert.equal(r.coverage,0);assert.equal(r.score,20);assert.equal(r.missing.length,2);
});
test('no reference or empty input is not silently graded',()=>{
 assert.equal(reviewSummary('',ref).empty,true);
 assert.equal(reviewSummary('Some words.',null).unavailable,true);
});
test('substring English matches do not count',()=>{
 const r=reviewSummary('A relationshipless story about unresigned officers.',ref);
 assert.equal(r.coverage,0);
});

test('two article notes persist independently and both clear for a new broadcast',()=>{
 const first={episodeId:'same',text:'First article summary'};
 const second={episodeId:'same',text:'Second article summary'};
 assert.equal(summaryNoteState(first,'same').text,'First article summary');
 assert.equal(summaryNoteState(second,'same').text,'Second article summary');
 for(const note of [first,second])assert.equal(summaryNoteState(note,'new').text,'');
});


test('transcript view uses full broadcast without pretending it is an article rubric',()=>{
 const first={transcript:'Estimated first article',keywords:[{label:'first',terms:['first']}]};
 const full={transcript:'Whole broadcast',start:0,end:300};
 const view=transcriptReference([first],full,1);
 assert.equal(view.transcript,'Whole broadcast');
 assert.equal(view.kind,'full_broadcast');
 assert.equal(reviewSummary('My article summary.',view).unavailable,true);
 assert.equal(transcriptReference([first],null,1),first);
 assert.equal(transcriptReference([],null,1),null);
});
