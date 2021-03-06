import sys

import mlflow
import pytest
import yaml
from kedro.extras.datasets.pickle import PickleDataSet
from kedro.io import DataCatalog, MemoryDataSet
from kedro.pipeline import Pipeline, node
from kedro.runner import SequentialRunner
from mlflow.tracking import MlflowClient

from kedro_mlflow.framework.context import get_mlflow_config
from kedro_mlflow.framework.hooks.pipeline_hook import (
    MlflowPipelineHook,
    _format_conda_env,
)
from kedro_mlflow.pipeline import pipeline_ml
from kedro_mlflow.pipeline.pipeline_ml import PipelineML


@pytest.fixture
def python_version():
    python_version = ".".join(
        [
            str(sys.version_info.major),
            str(sys.version_info.minor),
            str(sys.version_info.micro),
        ]
    )
    return python_version


@pytest.fixture
def requirements_path(tmp_path):
    return tmp_path / "requirements.txt"


@pytest.fixture
def requirements_path_str(requirements_path):
    return requirements_path.as_posix()


@pytest.fixture
def environment_path(tmp_path):
    return tmp_path / "environment.yml"


@pytest.fixture
def environment_path_str(environment_path):
    return environment_path.as_posix()


@pytest.fixture
def env_from_none(python_version):
    return dict(python=python_version)


@pytest.fixture
def env_from_requirements(requirements_path, python_version):
    requirements_data = ["pandas>=1.0.0,<2.0.0", "kedro==0.15.9"]
    with open(requirements_path, mode="w") as file_handler:
        for item in requirements_data:
            file_handler.write(f"{item}\n")
    return dict(python=python_version, dependencies=requirements_data)


@pytest.fixture
def env_from_dict(python_version):
    env_from_dict = dict(
        python=python_version, dependencies=["pandas>=1.0.0,<2.0.0", "kedro==0.15.9"]
    )
    return env_from_dict


@pytest.fixture
def env_from_environment(environment_path, env_from_dict):

    with open(environment_path, mode="w") as file_handler:
        yaml.dump(env_from_dict, file_handler)

    return env_from_dict


@pytest.mark.parametrize(
    "conda_env,expected",
    (
        [None, pytest.lazy_fixture("env_from_none")],
        [pytest.lazy_fixture("env_from_dict"), pytest.lazy_fixture("env_from_dict")],
        [
            pytest.lazy_fixture("requirements_path"),
            pytest.lazy_fixture("env_from_requirements"),
        ],
        [
            pytest.lazy_fixture("requirements_path_str"),
            pytest.lazy_fixture("env_from_requirements"),
        ],
        [
            pytest.lazy_fixture("environment_path"),
            pytest.lazy_fixture("env_from_environment"),
        ],
        [
            pytest.lazy_fixture("environment_path_str"),
            pytest.lazy_fixture("env_from_environment"),
        ],
    ),
)
def test_format_conda_env(conda_env, expected):
    conda_env = _format_conda_env(conda_env)
    assert conda_env == expected


def test_format_conda_env_error():
    with pytest.raises(ValueError, match="Invalid conda_env"):
        _format_conda_env(["invalid_list"])


@pytest.fixture
def dummy_pipeline():
    def preprocess_fun(data):
        return data

    def train_fun(data, param):
        return 2

    def predict_fun(model, data):
        return data * model

    dummy_pipeline = Pipeline(
        [
            node(
                func=preprocess_fun,
                inputs="raw_data",
                outputs="data",
                tags=["training", "inference"],
            ),
            node(
                func=train_fun,
                inputs=["data", "params:unused_param"],
                outputs="model",
                tags=["training"],
            ),
            node(
                func=predict_fun,
                inputs=["model", "data"],
                outputs="predictions",
                tags=["inference"],
            ),
        ]
    )
    return dummy_pipeline


@pytest.fixture
def dummy_pipeline_ml(dummy_pipeline):

    dummy_pipeline_ml = pipeline_ml(
        training=dummy_pipeline.only_nodes_with_tags("training"),
        inference=dummy_pipeline.only_nodes_with_tags("inference"),
        input_name="raw_data",
    )
    return dummy_pipeline_ml


@pytest.fixture
def dummy_catalog(tmp_path):
    dummy_catalog = DataCatalog(
        {
            "raw_data": MemoryDataSet(1),
            "params:unused_param": MemoryDataSet("blah"),
            "data": MemoryDataSet(),
            "model": PickleDataSet((tmp_path / "model.csv").as_posix()),
        }
    )
    return dummy_catalog


@pytest.fixture
def dummy_run_params(tmp_path):
    dummy_run_params = {
        "run_id": "abcdef",
        "project_path": tmp_path.as_posix(),
        "env": "local",
        "kedro_version": "0.16.0",
        "tags": [],
        "from_nodes": [],
        "to_nodes": [],
        "node_names": [],
        "from_inputs": [],
        "load_versions": [],
        "pipeline_name": "my_cool_pipeline",
        "extra_params": [],
    }
    return dummy_run_params


@pytest.fixture
def dummy_mlflow_conf(tmp_path):
    def _write_yaml(filepath, config):
        filepath.parent.mkdir(parents=True, exist_ok=True)
        yaml_str = yaml.dump(config)
        filepath.write_text(yaml_str)

    _write_yaml(
        tmp_path / "conf" / "base" / "mlflow.yml",
        dict(
            mlflow_tracking_uri=(tmp_path / "mlruns").as_posix(),
            experiment=dict(name="Default", create=True),
            run=dict(id=None, name=None, nested=True),
            ui=dict(port=None, host=None),
        ),
    )


@pytest.mark.parametrize(
    "pipeline_to_run",
    [
        (pytest.lazy_fixture("dummy_pipeline")),
        (pytest.lazy_fixture("dummy_pipeline_ml")),
    ],
)
def test_mlflow_pipeline_hook_with_different_pipeline_types(
    mocker,
    monkeypatch,
    tmp_path,
    config_dir,
    env_from_dict,
    pipeline_to_run,
    dummy_catalog,
    dummy_run_params,
    dummy_mlflow_conf,
):
    # config_with_base_mlflow_conf is a conftest fixture
    mocker.patch("kedro_mlflow.utils._is_kedro_project", return_value=True)
    monkeypatch.chdir(tmp_path)
    pipeline_hook = MlflowPipelineHook(conda_env=env_from_dict, model_name="model")
    runner = SequentialRunner()
    pipeline_hook.before_pipeline_run(
        run_params=dummy_run_params, pipeline=pipeline_to_run, catalog=dummy_catalog
    )
    runner.run(pipeline_to_run, dummy_catalog, dummy_run_params["run_id"])
    run_id = mlflow.active_run().info.run_id
    pipeline_hook.after_pipeline_run(
        run_params=dummy_run_params, pipeline=pipeline_to_run, catalog=dummy_catalog
    )
    # test : parameters should have been logged
    mlflow_conf = get_mlflow_config(tmp_path)
    mlflow_client = MlflowClient(mlflow_conf.mlflow_tracking_uri)
    run_data = mlflow_client.get_run(run_id).data
    # all run_params are recorded as tags
    for k, v in dummy_run_params.items():
        if v:
            assert run_data.tags[k] == str(v)
    # params are not recorded because we don't have MlflowNodeHook here
    # and the model should not be logged when it is not a PipelineML
    nb_artifacts = len(mlflow_client.list_artifacts(run_id))
    if isinstance(pipeline_to_run, PipelineML):
        assert nb_artifacts == 1
    else:
        assert nb_artifacts == 0


def test_generate_kedro_commands():
    # TODO : add a better test because the formatting of record_data is subject to change
    # We could check that the command is recored and then rerun properly
    record_data = {
        "tags": ["tag1", "tag2"],
        "from_nodes": ["node1"],
        "to_nodes": ["node3"],
        "node_names": ["node1", "node2", "node1"],
        "from_inputs": ["data_in"],
        "load_versions": {"data_inter": "01:23:45"},
        "pipeline_name": "fake_pl",
    }
    from kedro_mlflow.framework.hooks.pipeline_hook import _generate_kedro_command

    expected = "kedro run --from-inputs=data_in --from-nodes=node1 --to-nodes=node3 --node=node1,node2,node1 --pipeline=fake_pl --tag=tag1,tag2 --load-version=data_inter:01:23:45"
    assert _generate_kedro_command(**record_data) == expected
