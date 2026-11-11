const {test}=require('node:test');
const assert=require('node:assert/strict');
const catalog=require('../frontend/admin.js');
const model={id:'model-a',name:'部件检测',version:'2.0',enabled:true,task:'detect',classes:{0:'terminal',2:'螺丝'}};
const rule=()=>({id:'rule-a',name:'端子到位',class_id:0,kind:'required',min_count:2,max_count:4,threshold:.6,roi:[0,0,1,1]});
const body=()=>({name:'装配检查',status:'published',rules:[rule()]});
const scenes=[
 {id:'draft',name:'端子草稿',description:'工位 A',model,status:'draft',ready:false,rules:[rule()],updated_at:3},
 {id:'ready',name:'螺丝安装',description:'工位 B',model,status:'published',ready:true,rules:[rule(),{...rule(),id:'rule-b'}],updated_at:2},
 {id:'blocked',name:'装配复核',description:'工位 C',model,status:'published',ready:false,rules:[rule()],updated_at:1}
];
test('attention includes only unavailable published scenes; published includes both states',()=>{
 assert.deepEqual(catalog.scenes(scenes,'','attention').map(s=>s.id),['blocked']);
 assert.deepEqual(catalog.scenes(scenes,'','published').map(s=>s.id),['ready','blocked']);
 assert.deepEqual(catalog.scenes(scenes,'','draft').map(s=>s.id),['draft']);
});
test('search intersects status and includes model versions, rule names and descriptions',()=>{
 assert.equal(catalog.scenes(scenes,'  2.0  ','published').length,2);
 assert.deepEqual(catalog.scenes(scenes,'工位 A').map(s=>s.id),['draft']);
 assert.equal(catalog.scenes(scenes,'端子到位','draft').length,1);
 assert.equal(catalog.scenes(scenes,'does not exist').length,0);
});
test('sorting does not mutate the registry order',()=>{
 assert.deepEqual(catalog.scenes(scenes,'','all','rules').map(s=>s.id),['ready','draft','blocked']);
 assert.deepEqual(scenes.map(s=>s.id),['draft','ready','blocked']);
});
test('model search combines class, status and type filters, including sparse category IDs',()=>{
 const disabled={...model,id:'model-b',name:'旧模型',enabled:false,task:'classify',classes:{9:'Scratch'}};
 assert.deepEqual(catalog.models([model,disabled],'SCRATCH','disabled','classify').map(m=>m.id),['model-b']);
 assert.equal(catalog.models([model,disabled],'螺丝','enabled','detect').length,1);
 assert.equal(catalog.models([model,disabled],'Scratch','enabled').length,0);
 assert.deepEqual(catalog.models([model,disabled],'model-b').map(m=>m.id),['model-b']);
});
test('valid rules pass; drafts may omit positive evidence but cannot reference foreign categories',()=>{
 assert.deepEqual(catalog.validate(body(),model),[]);
 assert.deepEqual(catalog.validate({name:'草稿',status:'draft',rules:[]},model),[]);
 const invalid=body();invalid.rules[0].class_id=1;
 assert.ok(catalog.validate(invalid,model).some(issue=>issue.includes('类别不属于')));
});
test('publication requires enabled model and at least one required rule',()=>{
 const forbidden=body();forbidden.rules[0].kind='forbidden';
 assert.ok(catalog.validate(forbidden,model).some(issue=>issue.includes('必须检出')));
 assert.ok(catalog.validate(body(),{...model,enabled:false}).some(issue=>issue.includes('已停用')));
 assert.equal(catalog.validate({...forbidden,status:'draft'},model).length,0);
});
test('rejects zero-area, inverted, out-of-range and nonfinite regions before saving',()=>{
 for(const roi of [[0,0,0,1],[.8,0,.2,1],[0,-.1,1,1],[0,0,Infinity,1],[0,0,1,NaN]]){
  const value=body();value.rules[0].roi=roi;
  assert.ok(catalog.validate(value,model).some(issue=>issue.includes('检测区域')));
 }
});
test('rejects reversed or fractional counts, invalid thresholds and duplicate IDs',()=>{
 for(const patch of [{min_count:5,max_count:2},{min_count:1.5},{max_count:301},{threshold:NaN},{threshold:.01}]){
  const value=body();Object.assign(value.rules[0],patch);assert.ok(catalog.validate(value,model).length);
 }
 const value=body();value.rules.push(rule());assert.ok(catalog.validate(value,model).some(issue=>issue.includes('编号重复')));
});
test('classification rules enforce one result per region',()=>{
 assert.ok(catalog.validate(body(),{...model,task:'classify'}).some(issue=>issue.includes('分类模型')));
 const value=body();Object.assign(value.rules[0],{min_count:1,max_count:1});
 assert.deepEqual(catalog.validate(value,{...model,task:'classify'}),[]);
});
test('publication detects impossible region rules while drafts remain editable',()=>{
 const value=body();value.rules.push({...rule(),id:'forbidden',kind:'forbidden'});
 assert.ok(catalog.validate(value,model).some(issue=>issue.includes('无法同时通过')));
 assert.deepEqual(catalog.validate({...value,status:'draft'},model),[]);
 value.rules[1].threshold=.8;
 assert.deepEqual(catalog.validate(value,model),[]);
 value.rules[1]={...rule(),id:'other',min_count:5,max_count:6};
 assert.ok(catalog.validate(value,model).some(issue=>issue.includes('互相矛盾')));
 value.rules[1].roi=[.5,0,1,1];
 assert.deepEqual(catalog.validate(value,model),[]);
});
test('classification cannot require two different top classes in one region',()=>{
 const value=body();value.rules=[{...rule(),min_count:1,max_count:1},{...rule(),id:'other',class_id:2,min_count:1,max_count:1}];
 assert.ok(catalog.validate(value,{...model,task:'classify'}).some(issue=>issue.includes('不同类别')));
 assert.deepEqual(catalog.validate(value,model),[]);
});
