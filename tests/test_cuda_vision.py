import cv2
import numpy as np
import pytest
import torch
from algorithms import cuda_vision as v

pytestmark=pytest.mark.skipif(not torch.cuda.is_available(),reason='CUDA integration test')

def test_warp_matches_opencv_and_preserves_borders():
    image=np.random.default_rng(9).integers(0,256,(80,120,3),dtype=np.uint8)
    matrices=[np.eye(3),np.array([[1.1,.03,-12],[.02,.9,6],[.0003,-.0005,1.]])]
    actual=v.warp(v.rgb(image),np.stack(matrices),(100,70)).flip(1).permute(0,2,3,1).cpu().numpy()
    for matrix,result in zip(matrices,actual):
        expected=cv2.warpPerspective(image,matrix,(100,70))
        assert np.max(np.abs(result.astype(int)-expected.astype(int)))<=1


@pytest.mark.parametrize('size', [(100,70),(127,91),(240,180)])
@pytest.mark.parametrize('channels', [1,3])
def test_region_warp_is_exact_full_warp_slice_for_shapes_and_boundaries(size,channels):
    rng=np.random.default_rng(39)
    shape=(80,120) if channels==1 else (3,80,120)
    image=torch.as_tensor(rng.integers(0,256,shape,dtype=np.uint8),device='cuda')
    matrices=[np.eye(3),np.array([[1.1,.03,-12],[.02,.9,6],[.0003,-.0005,1.]]),
              np.array([[.91,-.08,15],[.06,1.07,-18],[-.0008,.0011,1.]])]
    # Include different patch sizes, clipped ends, negative Python slice bounds,
    # reversed/empty slices and output regions extending beyond the input image.
    regions=[(0,0,17,13),(11,9,69,51),(40,17,65,73),(-15,-20,300,250),
             (-400,-400,9,7),(size[0]-8,size[1]-5,size[0]+12,size[1]+20),
             (30,20,10,25),(0,8,10,8)]
    for matrix in matrices:
        expected=v.warp(image,matrix,size)[0]
        actual=v.warp_regions(image,matrix,size,regions)
        assert len(actual)==len(regions)
        for patch,(a,b,c,d) in zip(actual,regions):
            assert torch.equal(patch,expected[:,b:d,a:c])
    assert v.warp_regions(image,matrices[0],size,[])==[]


def test_region_warp_cached_grid_is_ready_across_cuda_streams():
    v._region_grid.cache_clear()
    image=torch.randint(0,256,(3,80,120),device='cuda',dtype=torch.uint8)
    matrix=np.array([[1.05,.02,-9],[.01,.93,5],[.0002,-.0003,1.]])
    size=(127,91);regions=[(7,8,49,61),(70,30,115,82)]
    producer,consumer=torch.cuda.Stream(),torch.cuda.Stream()
    input_ready=torch.cuda.Event();input_ready.record()
    with torch.cuda.stream(producer):
        producer.wait_event(input_ready)
        first=v.warp_regions(image,matrix,size,regions)
    with torch.cuda.stream(consumer):
        consumer.wait_event(input_ready)
        second=v.warp_regions(image,matrix,size,regions)
        consumed=torch.cuda.Event();consumed.record()
    torch.cuda.current_stream().wait_stream(producer)
    torch.cuda.current_stream().wait_event(consumed)
    expected=v.warp(image,matrix,size)[0]
    assert v._region_grid.cache_info().hits>=1
    for a,b,(x0,y0,x1,y1) in zip(first,second,regions):
        assert torch.equal(a,b)
        assert torch.equal(b,expected[:,y0:y1,x0:x1])


@pytest.mark.parametrize('eviction', ['lru','clear'])
def test_region_warp_grid_storage_survives_eviction_during_async_read(monkeypatch,eviction):
    if not hasattr(torch.cuda,'_sleep'):
        pytest.skip('CUDA delay helper unavailable')
    v._region_grid.cache_clear()
    device=torch.device('cuda',torch.cuda.current_device())
    image=torch.randint(0,256,(3,80,120),device=device,dtype=torch.uint8)
    matrix=torch.eye(3,device=device,dtype=torch.float64)
    size=(127,91);regions=((7,8,49,61),(70,30,115,82))
    points=sum((c-a)*(d-b) for a,b,c,d in regions)
    expected=v.warp(image,matrix,size)[0]
    input_ready=torch.cuda.Event();input_ready.record(torch.cuda.current_stream(device))
    producer,consumer=torch.cuda.Stream(device=device),torch.cuda.Stream(device=device)
    producer.wait_event(input_ready);consumer.wait_event(input_ready)
    try:
        # Warm both stream allocation pools before the deliberate delay. A new
        # cudaMalloc can synchronize and otherwise hide the lifetime violation.
        with torch.cuda.stream(consumer):
            warm=v.warp_regions(image,matrix,size,regions)
        consumer.synchronize();del warm
        v._region_grid.cache_clear()
        with torch.cuda.stream(producer):
            reserve=[torch.empty((3,points),device=device,dtype=torch.float64) for _ in range(32)]
        producer.synchronize();del reserve
        with torch.cuda.stream(producer):
            grid,ready=v._region_grid(device,regions)
            grid_pointer=grid.data_ptr()
            del grid,ready
        original_inverse=torch.linalg.inv
        def delayed_inverse(value):
            inverse=original_inverse(value)
            # inv synchronizes CUDA; delay afterwards so the grid read remains
            # queued when the host evicts the cache and reuses producer memory.
            torch.cuda._sleep(300_000_000)
            return inverse
        with monkeypatch.context() as patch:
            patch.setattr(torch.linalg,'inv',delayed_inverse)
            with torch.cuda.stream(consumer):
                actual=v.warp_regions(image,matrix,size,regions)
                consumed=torch.cuda.Event();consumed.record(consumer)
        with torch.cuda.stream(producer):
            if eviction=='lru':
                for shift in range(1,9):
                    v._region_grid(device,tuple((a+shift,b,c+shift,d) for a,b,c,d in regions))
            else:
                v._region_grid.cache_clear()
            clobbers=[torch.empty((3,points),device=device,dtype=torch.float64) for _ in range(16)]
            for value in clobbers:value.fill_(-999)
            pending=not consumed.query()
            if pending:
                assert grid_pointer not in [value.data_ptr() for value in clobbers], 'An in-flight grid was reused after cache eviction'
        consumer.synchronize();producer.synchronize()
        for patch,(a,b,c,d) in zip(actual,regions):
            assert torch.equal(patch,expected[:,b:d,a:c])
        if not pending:
            pytest.skip('Consumer completed before the eviction race window; retry on an idle GPU')
    finally:
        consumer.synchronize();producer.synchronize()
        v._region_grid.cache_clear()


def test_region_grid_ready_event_uses_tensor_device_when_another_device_is_current():
    if torch.cuda.device_count()<2:
        pytest.skip('Requires two CUDA devices')
    v._region_grid.cache_clear()
    current=torch.cuda.current_device()
    target=torch.device('cuda',1 if current==0 else 0)
    try:
        with torch.cuda.device(current):
            grid,ready=v._region_grid(target,((3,5,8,9),))
            assert torch.cuda.current_device()==current
            assert grid.device==target
            assert ready.device==target
            ready.synchronize()
            expected=torch.tensor([[x,y,1] for y in range(5,9) for x in range(3,8)],dtype=torch.float64).T
            assert torch.equal(grid.cpu(),expected)
    finally:
        torch.cuda.synchronize(target)
        v._region_grid.cache_clear()


def test_sticker_local_crops_match_default_full_locate():
    from algorithms.stickers import snapshot
    from algorithms.sticker_actions import StickerActionDetector
    from algorithms.cuda_actions import crop
    detector=StickerActionDetector(snapshot())
    image=v.rgb(detector.reference)
    full=detector.locate(image)
    local=detector.locate(image,local_crops=True)
    assert full is not None and local is not None
    aligned,matrix,inliers=full
    crops,local_matrix,local_inliers=local
    np.testing.assert_array_equal(matrix,local_matrix)
    assert inliers==local_inliers
    assert len(crops)==len(detector.config['targets'])
    for patch,target in zip(crops,detector.config['targets']):
        assert torch.equal(patch,crop(aligned,target))


@pytest.mark.parametrize('calibration', ['original','overhead-v4'])
def test_screw_component_regions_match_full_inspection_for_numpy_and_cuda_frame(monkeypatch,calibration):
    import copy
    from algorithms.screws import ROOT,snapshot
    from algorithms.screw_refined import RefinedScrewDetector
    frozen=copy.deepcopy(snapshot())
    if calibration!='original':
        profile=next(p for p in frozen['config']['calibrations'] if p['id']==calibration)
        frozen['config'].update(profile)
    detector=RefinedScrewDetector(frozen)
    image=cv2.imread(str(ROOT/frozen['config']['reference']))
    assert image is not None
    observed=[]
    def full_slices(frame,matrix,size,regions):
        aligned=v.warp(frame,matrix,size)[0]
        observed.append(tuple(regions))
        return [aligned[:,b:d,a:c] for a,b,c,d in regions]
    results=[]
    for supplied in (image,v.CudaFrame(v.rgb(image))):
        with monkeypatch.context() as patch:
            patch.setattr(v,'warp_regions',full_slices)
            expected=detector.inspect(supplied)
        actual=detector.inspect(supplied)
        expected.pop('inferenceMs');actual.pop('inferenceMs')
        assert expected['registered']
        assert len(expected['componentScores'])==len(frozen['config']['component_anchors'])
        assert actual==expected
        results.append(actual)
    assert len(observed)==2
    assert results[0]==results[1]


def test_correlation_matches_masked_and_unmasked_opencv():
    rng=np.random.default_rng(2)
    image=rng.integers(0,256,(60,60),dtype=np.uint8);template=image[6:54,6:54].copy()
    template[10:25,10:25]=rng.integers(0,256,(15,15),dtype=np.uint8)
    mask=np.ones((48,48),np.uint8);mask[12:36,12:36]=0
    for m in (None,mask):
        expected=cv2.matchTemplate(image,template,cv2.TM_CCOEFF_NORMED,mask=m).max()
        actual=v.correlation(torch.as_tensor(image,device='cuda'),torch.as_tensor(template,device='cuda'),
                             None if m is None else torch.as_tensor(m,device='cuda')).item()
        assert abs(actual-expected)<2e-4

def test_homography_with_outliers_and_blank_features():
    torch.manual_seed(4)
    source=torch.rand((80,2),device='cuda')*600
    matrix=source.new_tensor([[1.03,.01,15],[-.02,.95,9],[.0001,-.0002,1.]])
    target=v.transform(source,matrix);target[-24:]=torch.rand((24,2),device='cuda')*600
    fit=v.homography(source,target)
    assert fit is not None
    h,mask=fit
    assert int(mask.sum())==56
    assert torch.max(torch.abs(v.transform(source[:56],h)-target[:56]))<.01
    assert v.homography(source[:10],target[:10]) is None
    assert v.homography(torch.zeros_like(source),target) is None

def test_action_foreground_and_hog_match_trained_feature_contract():
    from algorithms import cuda_actions as a
    from algorithms.sticker_actions import foreground_mask,action_features
    rng=np.random.default_rng(17)
    ref=rng.integers(0,256,(180,180,3),dtype=np.uint8)
    image=ref.copy();image[40:130,30:140]=[210,180,130]
    gpu_mask=a.foreground(v.rgb(image),v.rgb(ref)).cpu().numpy()
    assert np.array_equal(gpu_mask,foreground_mask(image,ref))
    gray=rng.integers(0,256,(48,48),dtype=np.uint8)
    expected=cv2.HOGDescriptor((48,48),(16,16),(8,8),(8,8),9).compute(gray).flatten()
    actual=a.hog(torch.as_tensor(gray,device='cuda')[None])[0].cpu().numpy()
    assert np.max(abs(actual-expected))<2e-5
    actual,_=a.features([v.rgb(image)],[v.rgb(ref)])
    expected=action_features(image,ref)
    assert np.max(abs(actual.cpu().numpy()[0]-expected))<.005

def test_batched_resize_gray_keep_single_frame_results():
    frames=torch.randint(0,256,(5,3,80,80),dtype=torch.uint8,device='cuda')
    batched=v.gray(v.resize(frames,(48,48)))
    separate=torch.stack([v.gray(v.resize(frame,(48,48))) for frame in frames])
    assert torch.equal(batched,separate)


def test_batched_action_features_and_metadata_keep_scalar_patch_results():
    from algorithms import cuda_actions as a
    from algorithms.sticker_actions import foreground_mask
    rng=np.random.default_rng(83)
    references=[rng.integers(0,256,(180,180,3),dtype=np.uint8) for _ in range(8)]
    images=[reference.copy() for reference in references]
    for index,image in enumerate(images[1:],1):
        image[0:20+index*20,15:150]=[230,210,180]
    crops=[v.rgb(image) for image in images];refs=[v.rgb(image) for image in references]
    scalar_masks=[a.foreground(crop,ref) for crop,ref in zip(crops,refs)]
    masks=a.foreground(torch.stack(crops),torch.stack(refs))
    assert torch.equal(masks,torch.stack(scalar_masks))
    for image,reference,mask in zip(images,references,masks):
        np.testing.assert_array_equal(mask.cpu().numpy(),foreground_mask(image,reference))
    small=v.resize(torch.stack(crops)*torch.stack(scalar_masks).to(torch.uint8)[:,None],(48,48))
    color=small.flip(1).permute(0,2,3,1).flatten(1).float()/255
    occupancy=torch.stack([v.resize(mask,(18,18),area=True).flatten() for mask in scalar_masks])
    expected=torch.cat((color,a.hog(v.gray(small)),occupancy),1)
    features,feature_masks=a.features(crops,refs)
    assert torch.equal(features,expected)
    assert torch.equal(torch.stack(feature_masks),masks)
    expected_metadata=torch.stack([torch.cat((mask.mean().reshape(1),
        v.resize(v.gray(crop),(16,16)).flatten().float())) for crop,mask in zip(crops,scalar_masks)])
    assert torch.equal(a.observation_metadata(crops,feature_masks),expected_metadata)

def test_registration_handles_no_positive_peaks(monkeypatch):
    from types import SimpleNamespace
    registration=object.__new__(v.CudaRegistration)
    registration.count=2200;registration.device=torch.device('cuda')
    def empty(image,*,n,pad_if_not_divisible):
        assert n is None
        return [SimpleNamespace(keypoints=image.new_empty((0,2)),descriptors=image.new_empty((0,128)),
                                detection_scores=image.new_empty((0,)))]
    registration.model=empty
    features=registration.extract(torch.zeros((3,64,64),device='cuda',dtype=torch.uint8),(64,64))
    assert len(features[0])==0 and registration.match(features,features) is None

@pytest.mark.parametrize('kind', ['screw', 'sticker-action'])
def test_cuda_station_image_work_does_not_call_cpu_opencv(monkeypatch, kind):
    from algorithms.screws import ScrewDetector
    from algorithms.stickers import snapshot
    from algorithms.sticker_actions import StickerActionDetector
    if kind=='screw':
        detector=ScrewDetector()
    else:
        # Test the CUDA action backend explicitly. The active appearance
        # backend uses calibrated SIFT/OpenCV and has a different contract.
        import copy
        frozen=copy.deepcopy(snapshot())
        frozen['config'].update(mode='action',model='models/sticker-actions-v2.joblib',
                                action={'contact_threshold':.7})
        detector=StickerActionDetector(frozen)
    payload=cv2.imencode('.jpg',detector.reference)[1].tobytes()
    frame=v.decode(payload)
    detector.inspect(frame)
    with monkeypatch.context() as patch:
        def forbidden(*args,**kwargs):pytest.fail('CPU image work in CUDA station inference')
        for operation in ('imdecode','resize','cvtColor','warpPerspective','matchTemplate','SIFT_create','findHomography'):
            patch.setattr(cv2,operation,forbidden)
        patch.setattr(torch.cuda,'synchronize',forbidden)
        result=detector.inspect(frame)
    assert result['registered']
    assert any(t['state']!='UNKNOWN' for t in result['targets'])


def test_appearance_sticker_numpy_and_cuda_inputs_preserve_evidence():
    from algorithms.stickers import AppearanceStickerDetector
    detector=AppearanceStickerDetector()
    for image in (detector.reference, np.zeros_like(detector.reference), np.full_like(detector.reference,240)):
        expected=detector.inspect(image)
        actual=detector.inspect(v.CudaFrame(v.rgb(image)))
        expected.pop('inferenceMs');actual.pop('inferenceMs')
        assert actual==expected
        if image is not detector.reference:
            assert not actual['registered'] and not actual['displayTargets']
            assert all(t['state']=='UNKNOWN' for t in actual['targets'])

def test_cuda_lane_reuses_thread_rejects_queue_and_recovers_after_error():
    from algorithms.cuda_lane import run_in_lane
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from fastapi import HTTPException
    import threading
    owner=SimpleNamespace()
    first=run_in_lane(owner,threading.get_ident)
    with ThreadPoolExecutor(max_workers=2) as callers:
        assert callers.submit(run_in_lane,owner,threading.get_ident).result()==first
        entered,release=threading.Event(),threading.Event()
        def held():
            entered.set()
            assert release.wait(5)
            raise RuntimeError('model error')
        future=callers.submit(run_in_lane,owner,held)
        assert entered.wait(2)
        try:
            with pytest.raises(HTTPException) as failure:run_in_lane(owner,lambda:None)
            assert failure.value.status_code==429
        finally:release.set()
        with pytest.raises(RuntimeError,match='model error'):future.result()
        assert run_in_lane(owner,threading.get_ident)==first
    owner._cuda_executor.shutdown()
