import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('cleanup',Path(__file__).parents[1]/'deploy/cleanup-images.py')
cleanup=importlib.util.module_from_spec(spec);spec.loader.exec_module(cleanup)


def test_cleanup_preserves_running_newest_and_foreign_tags():
    images=[{'Id':str(i),'Created':str(i),'RepoTags':['ku-academic-mcp:'+str(i)*40]} for i in range(6)]
    images.append({'Id':'foreign','Created':'0','RepoTags':['ku-academic-mcp:'+'a'*40,'another:latest']})
    assert {i['Id'] for i in cleanup.select(images,{'0'})}=={'1','2'}
