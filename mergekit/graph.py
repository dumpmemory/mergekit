# Copyright (C) 2025 Arcee AI
# SPDX-License-Identifier: BUSL-1.1
"""
Module for computational graph execution.

Classes:
    Task: Abstract base class representing a computational task.
    Executor: Class for scheduling and executing directed acyclic task graphs.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union

import networkx
import torch
import tqdm
from pydantic import BaseModel
from typing_extensions import Generic, TypeVar

ValueT = TypeVar("ValueT")

logger = logging.getLogger(__name__)


class Task(ABC, BaseModel, Generic[ValueT], frozen=True):
    """
    Abstract base class representing a task in a computational graph.

    This class should be extended to define specific tasks. Each task can have arguments (dependencies) and a defined execution strategy.

    Attributes:
        Generic[ValueT] (TypeVar): The type of the value that the task returns upon execution.

    Methods:
        arguments: Abstract method to define task arguments (dependencies).
        execute: Abstract method to execute the task.
        priority: Returns the priority of the task for scheduling purposes.
        group_label: Returns an optional label for task grouping.
    """

    @abstractmethod
    def arguments(self) -> Dict[str, "Task"]:
        """
        Returns a dictionary of arguments required for this task. The keys of the dictionary
        are argument names, and the values are Task instances. These keys correspond to the
        keyword argument names expected by the execute method.

        For example, if this method returns {'input1': taskA, 'input2': taskB}, the execute
        method should expect to be called as execute(input1=valueA, input2=valueB), where
        valueA and valueB are the outputs of taskA and taskB respectively.

        Returns:
            Dict[str, "Task"]: A dictionary mapping argument names to Task instances.
        """
        ...

    @abstractmethod
    def execute(self, **kwargs) -> ValueT:
        """
        Executes the task using the results of its dependencies.

        The keyword arguments (**kwargs) for this method are dynamically determined based on
        the dictionary returned by the 'arguments' method. Each key in the 'arguments' method's
        return dictionary becomes a keyword argument in this method, with its value being
        the result of the corresponding task's execution.

        Returns:
            ValueT: The result of the task execution.
        """
        ...

    def priority(self) -> int:
        """
        Returns the priority of the task for scheduling.

        Higher numbers indicate higher priority. Default is 0.

        Returns:
            int: The priority of the task.
        """
        return 0

    def group_label(self) -> Optional[str]:
        """
        Returns an optional label used for grouping tasks together.

        Returns:
            Optional[str]: The group label of the task, if any.
        """
        return None

    def uses_accelerator(self) -> bool:
        """
        Returns True if the task can take advantage of matrix operation
        acceleration (such as on a GPU).
        """
        return False

    def main_thread_only(self) -> bool:
        """
        Returns True if the task should only be executed on the main thread.
        """
        return False


class Executor:
    """
    Schedules and executes a set of tasks and their dependencies.

    Handles scheduling, execution, the movement of data between devices, and the lifecycle of intermediate results.

    Attributes:
        math_device (torch.device): Device used for tensor computations.
        storage_device (torch.device): Device used for storing intermediate results.
        targets (List[Task]): List of target tasks to be executed.
        schedule (List[Task]): Calculated execution schedule of tasks.
        dependencies (Dict[Task, Set[Task]]): Dependencies of each task.
        cached_values (Optional[Dict[Task, Any]]): Cached values for tasks that have been executed before in a different context.
    """

    math_device: torch.device
    storage_device: torch.device
    targets: List[Task]
    schedule: List[Task]
    dependencies: Dict[Task, Set[Task]]
    cached_values: Optional[Dict[Task, Any]]

    def __init__(
        self,
        tasks: List[Task],
        math_device: torch.device = torch.device("cpu"),
        storage_device: torch.device = torch.device("cpu"),
        cached_values: Optional[Dict[Task, Any]] = None,
    ):
        """
        Initializes the Executor with a list of tasks and device configurations.

        Args:
            tasks (List[Task]): The list of tasks to be executed.
            math_device (torch.device, optional): The device for tensor computations. Defaults to CPU.
            storage_device (torch.device, optional): The device for storing results. Defaults to CPU.
        """
        self.cached_values = cached_values
        self.targets = tasks
        if isinstance(math_device, str):
            math_device = torch.device(math_device)
        if isinstance(storage_device, str):
            storage_device = torch.device(storage_device)
        self.math_device = math_device
        self.storage_device = storage_device
        self.schedule = self._make_schedule(tasks)

    def run(
        self,
        quiet: bool = False,
        desc: Optional[str] = None,
    ) -> Iterator[Tuple[Task, Any]]:
        """
        Execute the computed schedule and yield the target values.

        Yields:
            Iterator[Tuple[Task, Any]]: An iterator of task-result pairs.
        """
        # determine last usage of each value, so they can be evicted afterwards
        last_use_index = {}
        for idx, task in reversed(list(enumerate(self.schedule))):
            for t in self.dependencies.get(task, []):
                if t not in last_use_index:
                    last_use_index[t] = idx
            if task not in last_use_index:
                last_use_index[task] = idx
        for task in self.cached_values or []:
            if task not in last_use_index:
                last_use_index[task] = len(self.schedule) + 1

        values: Dict[Task, Any] = {}
        if self.cached_values:
            values.update(self.cached_values)
        for idx, task in (
            pbar := tqdm.tqdm(
                list(enumerate(self.schedule)),
                disable=quiet,
                desc=desc or "Executing graph",
            )
        ):
            use_math_device = task.uses_accelerator()

            arguments = {}
            for name, dep in task.arguments().items():
                value = values[dep]

                # ensure any input tensors are on math device if task asks for it
                if use_math_device:
                    value = self._move_tensors(value, self.math_device)

                arguments[name] = value
                del value

            res = task.execute(**arguments)
            del arguments
            res = self._move_tensors(res, self.storage_device)

            values[task] = res
            del res

            if task in self.targets:
                yield (task, values[task])

            # evict unreferenced values
            expired = []
            for key in values:
                if idx >= last_use_index[key]:
                    expired.append(key)

            for key in expired:
                del values[key]

        del values
        del pbar

    def execute(self, desc: Optional[str] = None) -> None:
        """
        Execute all tasks and discard results.
        """
        for task, value in self.run(desc=desc):
            pass

    def _move_tensors(
        self, value: Any, device: torch.device, non_blocking: Optional[bool] = None
    ) -> Any:
        if non_blocking is None:
            non_blocking = device.type == "cuda"
        if isinstance(value, torch.Tensor):
            return value.to(device=device, non_blocking=non_blocking)
        elif isinstance(value, dict):
            return {
                k: self._move_tensors(v, device, non_blocking) for k, v in value.items()
            }
        elif isinstance(value, list):
            return [self._move_tensors(v, device, non_blocking) for v in value]
        elif isinstance(value, tuple):
            return tuple(self._move_tensors(v, device, non_blocking) for v in value)
        return value

    DUMMY_TASK_VALUE = "!!DUMMY!!"

    def _make_schedule(self, targets: List[Task]) -> List[Task]:
        logger.debug(f"Building schedule for {len(targets)} targets")
        self.schedule = []
        self.dependencies = self._build_dependencies(targets)

        node_indices = {}
        node_values = []

        # instead of using the actual task objects as nodes in the graph,
        # use an integer index to avoid reserializing the task objects
        # inside networkx (slow)
        def _index(node: Union[Task, str]) -> int:
            if node not in node_indices:
                node_indices[node] = len(node_indices)
                node_values.append(node)
            return node_indices[node]

        edge_tups = []
        for node in self.dependencies:
            for dependency in self.dependencies[node]:
                edge_tups.append((_index(dependency), _index(node)))

        # add edges from a dummy node to each target to guarantee
        # they will be included in the final schedule
        dummy_index = _index(Executor.DUMMY_TASK_VALUE)
        for task in targets:
            edge_tups.append((dummy_index, _index(task)))

        def _compare_key(node: int) -> Tuple[str, int]:
            task = node_values[node]
            if task == Executor.DUMMY_TASK_VALUE:
                return ("", 0)
            return (
                task.group_label() or "",
                -task.priority(),
            )

        graph = networkx.DiGraph(edge_tups)
        return [
            node_values[idx]
            for idx in networkx.lexicographical_topological_sort(
                graph, key=_compare_key
            )
            if (idx != dummy_index)
            and node_values[idx] not in (self.cached_values or {})
        ]

    def _build_dependencies(self, targets: List[Task]) -> Dict[Task, Set[Task]]:
        task_dependencies: Dict[Task, Set[Task]] = {}
        to_process = list(targets)
        while to_process:
            child = to_process.pop()
            if child in task_dependencies:
                continue

            task_dependencies[child] = set()
            if child in (self.cached_values or {}):
                continue
            for _, dep in child.arguments().items():
                task_dependencies[child].add(dep)
                to_process.append(dep)
        return task_dependencies
