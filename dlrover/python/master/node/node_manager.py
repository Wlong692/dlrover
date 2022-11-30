# Copyright 2022 The DLRover Authors. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import threading
import time
from typing import Dict, List

from dlrover.python.common.constants import (
    DistributionStrategy,
    NodeEventType,
    NodeExitReason,
    NodeResourceLimit,
    NodeStatus,
    NodeType,
)
from dlrover.python.common.log_utils import default_logger as logger
from dlrover.python.master.node.event_callback import (
    ClusterContext,
    NodeEventCallback,
)
from dlrover.python.master.node.ps import ParameterServerManager
from dlrover.python.master.node.status_flow import (
    NodeStateFlow,
    get_node_state_flow,
)
from dlrover.python.master.node.training_node import (
    get_critical_worker_index,
    set_critical_node,
)
from dlrover.python.master.node.worker import (
    ChiefManager,
    EvaluatorManager,
    WorkerManager,
)
from dlrover.python.master.resource.job import JobResourceConfig
from dlrover.python.master.resource.optimizer import ResourcePlan
from dlrover.python.master.watcher.base_watcher import Node, NodeEvent
from dlrover.python.master.watcher.factory import new_node_watcher
from dlrover.python.scheduler.factory import new_elastic_job

_MAX_POD_RELAUNCH_COUNT = 5


class NodeManager(object):
    def __init__(
        self,
        job_resource: JobResourceConfig,
        job_name,
        namespace,
        relaunch_on_worker_failure=0,
        ps_is_critical=True,
        critical_worker_index={},
        wait_pending_relaunch=False,
        ps_relaunch_max_num=1,
        use_ddp=False,
        speed_monitor=None,
        engine="k8s",
    ):
        self._job_resource = job_resource
        self._relaunch_on_worker_failure = min(
            relaunch_on_worker_failure, _MAX_POD_RELAUNCH_COUNT
        )
        self._wait_pending_relaunch = wait_pending_relaunch
        self._start_launch_waiting_workers_time = time.time()
        self._stop_launch_worker_for_ps = False
        self._ps_is_critical = ps_is_critical
        self._critical_worker_index = critical_worker_index
        self._ps_relaunch_max_num = min(
            ps_relaunch_max_num, _MAX_POD_RELAUNCH_COUNT
        )
        self._use_ddp = use_ddp
        self._node_event_callbacks: List[NodeEventCallback] = []
        self._chief_worker_started = False
        self._stop_monitor = False
        self._last_pod_stats = None
        self._speed_monitor = speed_monitor

        # Protects followed variables, which are accessed from event_cb.
        self._lock = threading.Lock()
        self._job_nodes: Dict[str, Dict[int, Node]] = {}
        self._job_uuid = None
        self._ps_manager = None

        self._elastic_job = new_elastic_job(engine, job_name, namespace)
        self._node_watcher = new_node_watcher(engine, job_name, namespace)
        self._init_training_node_manager()

    def start(self):
        self._job_uuid = self._elastic_job.get_job_uuid()
        self._init_job_nodes()
        threading.Thread(
            target=self._monitor_nodes, name="node_monitor", daemon=True
        ).start()

    def _init_training_node_manager(self):
        self._ps_manager = ParameterServerManager(
            self._job_nodes.get(NodeType.PS, {}),
            self._job_resource,
            self._ps_relaunch_max_num,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )
        self._chief_manager = ChiefManager(
            self._job_nodes.get(NodeType.CHIEF, {}),
            self._job_resource,
            self._ps_relaunch_max_num,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )
        self._worker_manager = WorkerManager(
            self._job_nodes.get(NodeType.WORKER, {}),
            self._job_resource,
            self._ps_relaunch_max_num,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )
        self._evaluator_manager = EvaluatorManager(
            self._job_nodes.get(NodeType.EVALUATOR, {}),
            self._job_resource,
            self._ps_relaunch_max_num,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )

    def get_job_uuid(self):
        return self._job_uuid

    def add_node_event_callback(self, pod_event_callback):
        self._node_event_callbacks.append(pod_event_callback)

    def _init_job_nodes(self):
        self._job_nodes = self._job_resource.init_job_node_meta(
            self._relaunch_on_worker_failure,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )

        # worker and eval ids for pods that should be created
        # after all ps are running.
        self._workers_waiting_ps_running = []

        # ps pod ids that are deleted and waiting for relaunch
        self._deleted_ps_pod_ids = []

        self._relaunch_pod = True
        self._pending_relaunch_count = 0

        set_critical_node(
            self._job_nodes,
            self._ps_is_critical,
            self._critical_worker_index,
            self._ps_relaunch_max_num,
        )
        self._ps_manager.update_nodes(self._job_nodes.get(NodeType.PS, {}))
        self._chief_manager.update_nodes(
            self._job_nodes.get(NodeType.CHIEF, {})
        )
        self._worker_manager.update_nodes(
            self._job_nodes.get(NodeType.WORKER, {})
        )
        self._evaluator_manager.update_nodes(
            self._job_nodes.get(NodeType.EVALUATOR, {})
        )

    def _monitor_nodes(self):
        while True:
            nodes = self._node_watcher.list()
            self._process_list_nodes(nodes)
            try:
                if self._stop_monitor:
                    logger.info("Stop processing node events")
                    break
                for event in self._node_watcher.watch():
                    try:
                        self._process_event(event)
                    except Exception as e:
                        logger.warning(e)
            except Exception as e:
                logger.warning(e)
                time.sleep(30)

    def _process_list_nodes(self, nodes: List[Node]):
        """Callback with pod list by the list api of k8s."""
        exist_pods: Dict[str, List[int]] = {}
        for node_type in self._job_nodes.keys():
            exist_pods[node_type] = []
        for node in nodes:
            exist_pods[node.type].append(node.id)
            if node.status == NodeStatus.DELETED:
                type = NodeEventType.DELETED
            else:
                type = NodeEventType.MODIFIED
            # Mock event to avoid missing events
            event = NodeEvent(type, node)
            self._process_event(event)

        for node_type in self._job_nodes.keys():
            for node_id, node in self._job_nodes[node_type].items():
                if (
                    node.status != NodeStatus.INITIAL
                    and not node.is_released
                    and node_id not in exist_pods[node_type]
                ):
                    logger.info(
                        "Node %s %s is deleted without the event",
                        node_type,
                        node_id,
                    )
                    node.is_released = True

    def _process_event(self, event: NodeEvent):
        node_type = event.node.type
        node_id = event.node.id
        cur_node = self._job_nodes[node_type][node_id]
        cur_node.update_info(
            name=event.node.name,
            start_time=event.node.start_time,
            create_time=event.node.create_time,
        )

        # For the given node id, check whether it meets
        # the state change condition
        new_status = event.node.status
        with self._lock:
            old_status = cur_node.status
            status_change_flow: NodeStateFlow = get_node_state_flow(
                old_status, event.event_type, new_status
            )
            cur_node.update_status(new_status)
            # If there is no matched state change, return directly
            # If the pod has been succeed, return directly
            if (
                status_change_flow is None
                or status_change_flow.from_status == NodeStatus.SUCCEEDED
            ):
                return

            # Update the pod status for pod_info
            new_status = status_change_flow.to_status
            cur_node.set_exit_reason(event.node.exit_reason)
            self._process_node_events(status_change_flow, cur_node)

            should_relaunch = self._should_relaunch(
                cur_node, status_change_flow
            )
            if should_relaunch and self._wait_pending_relaunch:
                self._pending_relaunch_count += 1

        logger.info(
            "%s status change: %s to %s, by evt_type %s, phase %s",
            cur_node.name,
            old_status,
            new_status,
            event.event_type,
            new_status,
        )

        if should_relaunch:
            self._relaunch_typed_pod(cur_node)

    def _process_node_events(
        self, status_change_flow: NodeStateFlow, node: Node
    ):
        cluster_context = ClusterContext(node_manager=self)
        if status_change_flow.to_status == NodeStatus.RUNNING:
            [
                callback.on_node_started(node, cluster_context)
                for callback in self._node_event_callbacks
            ]
        elif status_change_flow.to_status == NodeStatus.SUCCEEDED:
            [
                callback.on_node_succeeded(node, cluster_context)
                for callback in self._node_event_callbacks
            ]
        elif status_change_flow.to_status == NodeStatus.FAILED:
            [
                callback.on_node_failed(node, cluster_context)
                for callback in self._node_event_callbacks
            ]
        elif (
            status_change_flow.from_status != NodeStatus.FAILED
            and status_change_flow.from_status != NodeStatus.SUCCEEDED
            and status_change_flow.to_status == NodeStatus.DELETED
        ):
            [
                callback.on_node_deleted(node, cluster_context)
                for callback in self._node_event_callbacks
            ]

    def _should_relaunch(self, node: Node, status_change_flow: NodeStateFlow):
        should_relaunch = (
            status_change_flow.should_relaunch
            and self._relaunch_pod
            and node.relaunchable
        )
        if should_relaunch:
            if node.exit_reason == NodeExitReason.FATAL_ERROR:
                should_relaunch = False
            elif node.exit_reason == NodeExitReason.OOM:
                mem = node.used_resource.memory
                if mem > NodeResourceLimit.MAX_MEMORY:
                    should_relaunch = False
                    logger.warning(
                        "The memory of worker %s is beyond the limit %s MB.",
                        mem,
                        NodeResourceLimit.MAX_MEMORY,
                    )
                elif node.relaunch_count >= node.max_relaunch_count:
                    should_relaunch = False
                    logger.warning(
                        "The relaunched count %s is beyond the maximum %s.",
                        node.relaunch_count,
                        node.max_relaunch_count,
                    )
                else:
                    node.is_recovered_oom = True
            elif node.exit_reason != NodeExitReason.KILLED:
                if node.relaunch_count > node.max_relaunch_count:
                    logger.warning(
                        "The relaunch count for Error has been exhausted."
                    )
                    should_relaunch = False
        if should_relaunch:
            node.inc_relaunch_count()

        return should_relaunch

    def _relaunch_typed_pod(self, node: Node):
        logger.info("Relaunch the pod: {}".format(node.name))

    def all_workers_exited(self):
        return (
            self._chief_manager.all_nodes_exited()
            and self._worker_manager.all_nodes_exited()
            and self._evaluator_manager.all_nodes_exited()
        )

    def all_workers_failed(self):
        return (
            self._chief_manager.all_nodes_failed()
            and self._worker_manager.all_nodes_failed()
            and self._evaluator_manager.all_nodes_failed()
        )

    def all_workers_deleted(self):
        return (
            self._chief_manager.all_nodes_deleted()
            and self._worker_manager.all_nodes_deleted()
            and self._evaluator_manager.all_nodes_deleted()
        )

    def all_critical_node_completed(self):
        alive_critical_nodes = []
        for _, nodes in self._job_nodes.items():
            for node_id, node in nodes.items():
                if node.critical and node.status in [
                    NodeStatus.INITIAL,
                    NodeStatus.PENDING,
                    NodeStatus.RUNNING,
                ]:
                    alive_critical_nodes.append(node_id)

        completed = not alive_critical_nodes
        if not completed:
            logger.info("Critical pods %s are running.", alive_critical_nodes)
        return completed

    def remove_worker(self, worker_id):
        if self._job_nodes[NodeType.WORKER][worker_id].critical:
            logger.info("Skip the critical worker %s", worker_id)
        else:
            logger.info("Delete worker %s", worker_id)
            plan = self._worker_manager.remove_node(worker_id)
            logger.info("plan %s", plan)

    def get_running_nodes(self):
        nodes = self._chief_manager.get_running_nodes()
        nodes.extend(self._worker_manager.get_running_nodes())
        nodes.extend(self._evaluator_manager.get_running_nodes())
        nodes.extend(self.get_cur_cluster_ps())
        return nodes

    def stop(self):
        self._relaunch_pod = False
        with self._lock:
            for node_type in self._job_nodes.keys():
                for node in self._job_nodes[node_type].values():
                    node.critical = False
                    node.is_released = True
        self._stop_monitor = True

    def update_node_resource_usage(self, node_type, node_id, cpu, memory):
        node = self._job_nodes[node_type][node_id]
        node.update_resource_usage(cpu, memory)

    # TODO: implement the function.
    def start_auto_scale(self):
        """Start to auto scale"""
        pass

    def get_cur_cluster_ps(self):
        """Get PS nodes in the current training cluster."""
        return self._ps_manager.get_training_ps_cluster()

    def get_next_cluster_ps(self):
        """Get PS nodes in the next training cluster."""
        return self._ps_manager.get_next_training_ps_cluster()

    def ready_for_new_ps_cluster(self):
        return self._ps_manager.get_ready_for_new_ps_cluster()

    def remove_training_nodes(self):
        """Remove all PS and workers"""
        plan = ResourcePlan()
        training_nodes = list(
            self._job_nodes[NodeType.WORKER].values()
        ) + list(self._job_nodes[NodeType.PS].values())
        for node in training_nodes:
            if (
                node.status in [NodeStatus.RUNNING, NodeStatus.PENDING]
                and not node.is_released
            ):
                node.critical = False
                node.relaunchable = False
                node.is_released = True
                node.status = NodeStatus.DELETED
                logger.info("Remove node %s", node.name)
                plan.removed_nodes.append(node.name)


def create_node_manager(args, speed_monitor) -> NodeManager:
    # relaunch on worker failure for PS or custom strategy
    if (
        args.distribution_strategy != DistributionStrategy.PARAMETER_SERVER
        and args.distribution_strategy != DistributionStrategy.CUSTOM
    ):
        args.relaunch_on_worker_failure = 0

    job_resource = JobResourceConfig()
    job_resource.add_node_group_resource(
        NodeType.WORKER,
        args.num_workers,
        args.worker_resource_request,
        args.worker_pod_priority,
    )
    job_resource.add_node_group_resource(
        NodeType.PS,
        args.num_ps_pods,
        args.ps_resource_request,
        args.ps_pod_priority,
    )
    # Keep the same as the worker.
    evaluator_pod_priority = (
        args.evaluator_pod_priority
        if args.evaluator_pod_priority == "low"
        else "high"
    )
    job_resource.add_node_group_resource(
        NodeType.EVALUATOR,
        args.num_evaluators,
        args.evaluator_resource_request,
        evaluator_pod_priority,
    )
    job_resource.add_node_group_resource(
        NodeType.CHIEF,
        args.num_tf_master,
        args.tf_master_resource_request,
        args.tf_master_pod_priority,
    )
    critical_worker_index = get_critical_worker_index(args)
    # Custom distribution strategy does not exit if there are pending pods
    wait_pending_relaunch = (
        args.distribution_strategy == DistributionStrategy.CUSTOM
    )

    return NodeManager(
        job_resource=job_resource,
        job_name=args.job_name,
        namespace=args.namespace,
        relaunch_on_worker_failure=args.relaunch_on_worker_failure,
        ps_is_critical=args.ps_is_critical,
        critical_worker_index=critical_worker_index,
        wait_pending_relaunch=wait_pending_relaunch,
        ps_relaunch_max_num=args.ps_relaunch_max_num,
        use_ddp=args.use_ddp,
        speed_monitor=speed_monitor,
    )