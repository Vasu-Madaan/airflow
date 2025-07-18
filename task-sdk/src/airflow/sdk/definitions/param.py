# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import contextlib
import copy
import json
import logging
from collections.abc import ItemsView, Iterable, Mapping, MutableMapping, ValuesView
from typing import TYPE_CHECKING, Any, ClassVar

from airflow.exceptions import AirflowException, ParamValidationError
from airflow.sdk.definitions._internal.mixins import ResolveMixin
from airflow.utils.types import NOTSET, ArgNotSet

if TYPE_CHECKING:
    from airflow.sdk.definitions.context import Context
    from airflow.sdk.definitions.dag import DAG
    from airflow.sdk.types import Operator

logger = logging.getLogger(__name__)


class Param:
    """
    Class to hold the default value of a Param and rule set to do the validations.

    Without the rule set it always validates and returns the default value.

    :param default: The value this Param object holds
    :param description: Optional help text for the Param
    :param schema: The validation schema of the Param, if not given then all kwargs except
        default & description will form the schema
    """

    __version__: ClassVar[int] = 1

    CLASS_IDENTIFIER = "__class"

    def __init__(self, default: Any = NOTSET, description: str | None = None, **kwargs):
        if default is not NOTSET:
            self._check_json(default)
        self.value = default
        self.description = description
        self.schema = kwargs.pop("schema") if "schema" in kwargs else kwargs

    def __copy__(self) -> Param:
        return Param(self.value, self.description, schema=self.schema)

    @staticmethod
    def _check_json(value):
        try:
            json.dumps(value)
        except Exception:
            raise ParamValidationError(
                f"All provided parameters must be json-serializable. The value '{value}' is not serializable."
            )

    def resolve(self, value: Any = NOTSET, suppress_exception: bool = False) -> Any:
        """
        Run the validations and returns the Param's final value.

        May raise ValueError on failed validations, or TypeError
        if no value is passed and no value already exists.
        We first check that value is json-serializable; if not, warn.
        In future release we will require the value to be json-serializable.

        :param value: The value to be updated for the Param
        :param suppress_exception: To raise an exception or not when the validations fails.
            If true and validations fails, the return value would be None.
        """
        import jsonschema
        from jsonschema import FormatChecker
        from jsonschema.exceptions import ValidationError

        if value is not NOTSET:
            self._check_json(value)
        final_val = self.value if value is NOTSET else value
        if isinstance(final_val, ArgNotSet):
            if suppress_exception:
                return None
            raise ParamValidationError("No value passed and Param has no default value")
        try:
            jsonschema.validate(final_val, self.schema, format_checker=FormatChecker())
        except ValidationError as err:
            if suppress_exception:
                return None
            raise ParamValidationError(err) from None
        self.value = final_val
        return final_val

    def dump(self) -> dict:
        """Dump the Param as a dictionary."""
        out_dict: dict[str, str | None] = {
            self.CLASS_IDENTIFIER: f"{self.__module__}.{self.__class__.__name__}"
        }
        out_dict.update(self.__dict__)
        # Ensure that not set is translated to None
        if self.value is NOTSET:
            out_dict["value"] = None
        return out_dict

    @property
    def has_value(self) -> bool:
        return self.value is not NOTSET and self.value is not None

    def serialize(self) -> dict:
        return {"value": self.value, "description": self.description, "schema": self.schema}

    @staticmethod
    def deserialize(data: dict[str, Any], version: int) -> Param:
        if version > Param.__version__:
            raise TypeError("serialized version > class version")

        return Param(default=data["value"], description=data["description"], schema=data["schema"])


class ParamsDict(MutableMapping[str, Any]):
    """
    Class to hold all params for dags or tasks.

    All the keys are strictly string and values are converted into Param's object
    if they are not already. This class is to replace param's dictionary implicitly
    and ideally not needed to be used directly.


    :param dict_obj: A dict or dict like object to init ParamsDict
    :param suppress_exception: Flag to suppress value exceptions while initializing the ParamsDict
    """

    __version__: ClassVar[int] = 1
    __slots__ = ["__dict", "suppress_exception"]

    def __init__(self, dict_obj: Mapping[str, Any] | None = None, suppress_exception: bool = False):
        self.__dict = {k: v if isinstance(v, Param) else Param(v) for k, v in (dict_obj or {}).items()}
        self.suppress_exception = suppress_exception

    def __bool__(self) -> bool:
        return bool(self.__dict)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ParamsDict):
            return self.dump() == other.dump()
        if isinstance(other, dict):
            return self.dump() == other
        return NotImplemented

    def __copy__(self) -> ParamsDict:
        return ParamsDict(self.__dict, self.suppress_exception)

    def __deepcopy__(self, memo: dict[int, Any] | None) -> ParamsDict:
        return ParamsDict(copy.deepcopy(self.__dict, memo), self.suppress_exception)

    def __contains__(self, o: object) -> bool:
        return o in self.__dict

    def __len__(self) -> int:
        return len(self.__dict)

    def __delitem__(self, v: str) -> None:
        del self.__dict[v]

    def __iter__(self):
        return iter(self.__dict)

    def __repr__(self):
        return repr(self.dump())

    def __setitem__(self, key: str, value: Any) -> None:
        """
        Override for dictionary's ``setitem`` method to ensure all values are of Param's type only.

        :param key: A key which needs to be inserted or updated in the dict
        :param value: A value which needs to be set against the key. It could be of any
            type but will be converted and stored as a Param object eventually.
        """
        if isinstance(value, Param):
            param = value
        elif key in self.__dict:
            param = self.__dict[key]
            try:
                param.resolve(value=value, suppress_exception=self.suppress_exception)
            except ParamValidationError as ve:
                raise ParamValidationError(f"Invalid input for param {key}: {ve}") from None
        else:
            # if the key isn't there already and if the value isn't of Param type create a new Param object
            param = Param(value)

        self.__dict[key] = param

    def __getitem__(self, key: str) -> Any:
        """
        Override for dictionary's ``getitem`` method to call the resolve method after fetching the key.

        :param key: The key to fetch
        """
        param = self.__dict[key]
        return param.resolve(suppress_exception=self.suppress_exception)

    def get_param(self, key: str) -> Param:
        """Get the internal :class:`.Param` object for this key."""
        return self.__dict[key]

    def items(self):
        return ItemsView(self.__dict)

    def values(self):
        return ValuesView(self.__dict)

    def update(self, *args, **kwargs) -> None:
        if len(args) == 1 and not kwargs and isinstance(args[0], ParamsDict):
            return super().update(args[0].__dict)
        super().update(*args, **kwargs)

    def dump(self) -> dict[str, Any]:
        """Dump the ParamsDict object as a dictionary, while suppressing exceptions."""
        return {k: v.resolve(suppress_exception=True) for k, v in self.items()}

    def validate(self) -> dict[str, Any]:
        """Validate & returns all the Params object stored in the dictionary."""
        resolved_dict = {}
        try:
            for k, v in self.items():
                resolved_dict[k] = v.resolve(suppress_exception=self.suppress_exception)
        except ParamValidationError as ve:
            raise ParamValidationError(f"Invalid input for param {k}: {ve}") from None

        return resolved_dict

    def serialize(self) -> dict[str, Any]:
        return self.dump()

    @staticmethod
    def deserialize(data: dict, version: int) -> ParamsDict:
        if version > ParamsDict.__version__:
            raise TypeError("serialized version > class version")

        return ParamsDict(data)


class DagParam(ResolveMixin):
    """
    DAG run parameter reference.

    This binds a simple Param object to a name within a DAG instance, so that it
    can be resolved during the runtime via the ``{{ context }}`` dictionary. The
    ideal use case of this class is to implicitly convert args passed to a
    method decorated by ``@dag``.

    It can be used to parameterize a DAG. You can overwrite its value by setting
    it on conf when you trigger your DagRun.

    This can also be used in templates by accessing ``{{ context.params }}``.

    **Example**:

        with DAG(...) as dag:
          EmailOperator(subject=dag.param('subject', 'Hi from Airflow!'))

    :param current_dag: Dag being used for parameter.
    :param name: key value which is used to set the parameter
    :param default: Default value used if no parameter was set.
    """

    def __init__(self, current_dag: DAG, name: str, default: Any = NOTSET):
        if default is not NOTSET:
            current_dag.params[name] = default
        self._name = name
        self._default = default
        self.current_dag = current_dag

    def iter_references(self) -> Iterable[tuple[Operator, str]]:
        return ()

    def resolve(self, context: Context) -> Any:
        """Pull DagParam value from DagRun context. This method is run during ``op.execute()``."""
        with contextlib.suppress(KeyError):
            if context["dag_run"].conf:
                return context["dag_run"].conf[self._name]
        if self._default is not NOTSET:
            return self._default
        with contextlib.suppress(KeyError):
            return context["params"][self._name]
        raise AirflowException(f"No value could be resolved for parameter {self._name}")

    def serialize(self) -> dict:
        """Serialize the DagParam object into a dictionary."""
        return {
            "dag_id": self.current_dag.dag_id,
            "name": self._name,
            "default": self._default,
        }

    @classmethod
    def deserialize(cls, data: dict, dags: dict) -> DagParam:
        """
        Deserializes the dictionary back into a DagParam object.

        :param data: The serialized representation of the DagParam.
        :param dags: A dictionary of available DAGs to look up the DAG.
        """
        dag_id = data["dag_id"]
        # Retrieve the current DAG from the provided DAGs dictionary
        current_dag = dags.get(dag_id)
        if not current_dag:
            raise ValueError(f"DAG with id {dag_id} not found.")

        return cls(current_dag=current_dag, name=data["name"], default=data["default"])


def process_params(
    dag: DAG,
    task: Operator,
    dagrun_conf: dict[str, Any] | None,
    *,
    suppress_exception: bool,
) -> dict[str, Any]:
    """Merge, validate params, and convert them into a simple dict."""
    from airflow.configuration import conf

    dagrun_conf = dagrun_conf or {}

    params = ParamsDict(suppress_exception=suppress_exception)
    with contextlib.suppress(AttributeError):
        params.update(dag.params)
    if task.params:
        params.update(task.params)
    if conf.getboolean("core", "dag_run_conf_overrides_params") and dagrun_conf:
        logger.debug("Updating task params (%s) with DagRun.conf (%s)", params, dagrun_conf)
        params.update(dagrun_conf)
    return params.validate()
