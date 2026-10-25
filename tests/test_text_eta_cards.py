"""Word ETA scopes and live updates agree without replacing card content."""
from pathlib import Path
import subprocess
import unittest

from fastapi import HTTPException

from app.library.router import _novel_reading, _reading_items, reading_estimates
from app.stats.estimates import build_estimator
from app.templating import templates
from tests.test_shelf_reading import ANovelsShelfIsItsChapters, ShelfCase

ROOT = Path(__file__).resolve().parent.parent


class JsonRequest:
    def __init__(self, targets):
        self.targets = targets

    async def json(self):
        return {'targets': self.targets}


class AuthoritativeEstimates(ShelfCase):
    chapter_words = ANovelsShelfIsItsChapters.chapter_words
    word_pace = ANovelsShelfIsItsChapters.word_pace
    anchor = ANovelsShelfIsItsChapters.anchor

    async def estimates(self, targets, user_id=1):
        return (await reading_estimates(JsonRequest(targets), {'id':user_id},self.db))['estimates']

    async def test_endpoint_keeps_reading_volume_and_home_novel_scopes_distinct(self):
        await self.word_pace()
        await self.chapter_words(1,100,volume=1)
        await self.chapter_words(2,200,volume=1)
        await self.chapter_words(3,1000,volume=2)
        await self.anchor(1,offset=40)
        result=await self.estimates([{'kind':'novel','id':1,'volume_id':101},{'kind':'novel','id':1}])
        self.assertEqual([(v['key'],v['remaining']) for v in result], [('novel:1:101',28),('novel:1:',128)])
        estimator=await build_estimator(self.db,1)
        self.assertAlmostEqual((await _novel_reading(self.db,1,estimator))[1].remaining,result[1]['remaining'])
        entry,=await _reading_items(self.db,1,estimator)
        self.assertAlmostEqual(entry['eta'].remaining,result[0]['remaining'])

    async def test_unknown_or_other_accounts_pace_cannot_supply_a_number(self):
        await self.word_pace()
        await self.chapter_words(1)
        await self.db.execute("INSERT INTO users(id,username,password_hash) VALUES (2,'other','x')")
        await self.db.commit()
        result=await self.estimates([{'kind':'novel','id':1},{'kind':'novel','id':999}],user_id=2)
        self.assertTrue(result[0]['collecting'])
        self.assertIsNone(result[0]['remaining'])
        self.assertFalse(result[1]['collecting'])

    async def test_collecting_label_and_authoritative_scope_are_rendered(self):
        await self.chapter_words(1)
        await self.anchor(1)
        entries=await _reading_items(self.db,1)
        source=templates.get_template('library/_reading_grid.html').render(reading_items=entries,user={'cover_blur_px':0})
        self.assertIn('Collecting data',source)
        self.assertIn('data-text-eta-kind="novel"',source)
        self.assertIn('data-text-eta-volume="101"',source)
        self.assertNotIn('data-eta-total=',source)

    async def test_completed_scope_is_not_reported_as_collecting(self):
        await self.chapter_words(1)
        await self.mark_read(1)
        result,=await self.estimates([{'kind':'novel','id':1}])
        self.assertIsNone(result['remaining'])
        self.assertFalse(result['collecting'])

    async def test_batches_are_bounded_and_scope_ids_are_strict(self):
        for targets in ([],[{'kind':'novel','id':1}]*101,[{'kind':'novel','id':True}],
                        [{'kind':'item','id':1,'volume_id':1}],[{'kind':'novel','id':1,'volume_id':-1}]):
            with self.subTest(targets=targets[:1]), self.assertRaises(HTTPException) as caught:
                await self.estimates(targets)
            self.assertEqual(caught.exception.status_code,400)


class LiveTextEstimates(unittest.TestCase):
    def run_js(self, test):
        source=(ROOT/'app/static/js/library-events.js').read_text()
        functions=source[source.index('    let textEstimatesBusy ='):source.index('    function applyProgress(')]
        harness='''
const assert=require('node:assert/strict');
let queued=null;
function scheduleRefresh(key,run){ queued=run; }
function formatDuration(seconds){ return String(seconds); }
function makeNode(kind,id,volume){
    const card={dataset:{eta:'old'},cover:{identity:'same'}};
    return {dataset:{textEtaKind:kind,textEtaId:String(id),...(volume?{textEtaVolume:String(volume)}:{})},
        isConnected:true,textContent:'old',closest:()=>card,card};
}
let nodes=[];
const document={querySelectorAll:()=>nodes};
'''
        result=subprocess.run(['node','-'],input=harness+functions+'\n(async()=>{\n'+test+'\n})().catch(error=>{ console.error(error); process.exitCode=1; });',
                              text=True,capture_output=True,check=False,cwd=ROOT)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_live_refresh_keeps_scopes_covers_and_text_out_of_percent_math(self):
        self.run_js('''
const item=makeNode('item',7), whole=makeNode('novel',2), volume=makeNode('novel',2,20);
nodes=[item,whole,volume];
item.dataset.etaTotal='800';
applyEta({querySelector:()=>item},39);
assert.equal(item.textContent,'old','text ETA must not scale by percentage');
let submitted;
global.fetch=async(url,request)=>{
    assert.equal(url,'/library/reading-estimates');
    submitted=JSON.parse(request.body).targets;
    return {ok:true,json:async()=>({estimates:[
        {key:'item:7:',remaining:12600,total:20000,collecting:false},
        {key:'novel:2:',remaining:800,total:800,collecting:false},
        {key:'novel:2:20',remaining:null,total:null,collecting:true}]})};
};
await refreshTextEstimates();
assert.equal(submitted.length,3);
assert.equal(item.textContent,'~12600');
assert.equal(whole.textContent,'~800');
assert.equal(volume.textContent,'Collecting data');
assert.equal(item.card.dataset.eta,'20000');
assert.equal(item.card.cover.identity,'same');
''')

    def test_new_progress_invalidates_response_already_in_flight(self):
        self.run_js('''
const node=makeNode('item',7); nodes=[node];
let release;
global.fetch=()=>new Promise(resolve=>{release=resolve;});
const pending=refreshTextEstimates();
scheduleTextEstimates();
release({ok:true,json:async()=>({estimates:[{key:'item:7:',remaining:999,total:999,collecting:false}]})});
await pending;
assert.equal(node.textContent,'old','older request must not overwrite later progress');
assert.equal(typeof queued,'function');
global.fetch=async()=>({ok:true,json:async()=>({estimates:[{key:'item:7:',remaining:100,total:200,collecting:false}]})});
await queued();
assert.equal(node.textContent,'~100');
''')

    def test_duplicate_cards_share_one_target_and_removed_cards_are_not_updated(self):
        self.run_js('''
const a=makeNode('item',7),b=makeNode('item',7); nodes=[a,b];
b.isConnected=false;
global.fetch=async(url,request)=>{
    assert.equal(JSON.parse(request.body).targets.length,1);
    return {ok:true,json:async()=>({estimates:[{key:'item:7:',remaining:50,total:100,collecting:false}]})};
};
await refreshTextEstimates();
assert.equal(a.textContent,'~50'); assert.equal(b.textContent,'old');
''')


if __name__=='__main__':
    unittest.main()
