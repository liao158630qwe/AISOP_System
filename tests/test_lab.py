import math
from algorithms.lab import hand_shape, raised_hands


def points(closed=False):
    p=[[0.,0.,0.] for _ in range(21)]
    for offset,(m,j,t) in enumerate([(5,6,8),(9,10,12),(13,14,16),(17,18,20)]):
        p[m]=[offset,0,0];p[j]=[offset,1,0];p[t]=[offset+.2,0 if closed else 2,0]
    return p


def test_hand_geometry_and_rotation():
    for closed,expected in [(False,'OPEN'),(True,'CLOSED')]:
        p=points(closed)
        assert hand_shape(p)==expected
        rotated=[[-y,x,z] for x,y,z in p]
        assert hand_shape(rotated)==expected
    assert hand_shape([[0,0,0]]*21)=='UNKNOWN'


def test_raise_requires_visible_shoulder_and_wrist():
    k=[[.5,.5,1] for _ in range(17)];k[9]=[.5,.2,1]
    people=[{'trackId':1,'keypoints':k}]
    assert len(raised_hands(people))==1
    k[9][2]=.1
    assert raised_hands(people)==[]


def test_algorithm_selection_validation(authenticated):
    _,_,client=authenticated
    for query in ['test_algorithms=unknown','test_algorithms=raise,raise','test_algorithms=raise&station_id=s1']:
        assert client.post('/api/v1/realtime/frame?'+query,content=b'x',headers={'Content-Type':'image/jpeg'}).status_code==422
