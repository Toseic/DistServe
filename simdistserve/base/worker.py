"""
Worker class for simulation. One worker class manages a TP group.
"""
import random
import warnings
from collections import deque
from typing import Optional, List, Iterable, TYPE_CHECKING, Union, TypedDict, Literal
from uuid import UUID

from simdistserve.estimators.time_estimator import get_prefill_time, get_decode_time

if TYPE_CHECKING:
    from simdistserve.base.scheduler import Scheduler
    from simdistserve.base.request import Request


# TODO: (Refactor) Make this a configuration.
class WorkerConfig(TypedDict):
    """Behaviors of worker."""
    TP_Prefill: int  # Tensor parallelism for prefill (default = 1)
    TP_Decode: int  # Tensor parallelism for decode (default = 1)
    model_type: str  # Model type for prefill/decode time calculation (default = ModelType.opt_13b)
    prefill_max_batch_size: int  # Maximum number of prefill request in a batch (default = 10**7)
    decode_max_batch_size: int  # Maximum number of decode request in a batch (default = 10**7)
    prefill_max_tokens: int  # Max tokens in prefill iteration (default = 10**7)
    decode_max_tokens: int  # Max tokens in a iteration forward (default = 10**7)
    enable_chunked_prefill: Optional[bool]  # Enable memory pressure simulation (default = False)
    engine_type: Literal["distserve", "vllm"]  # Engine type for prefill/decode time calculation (default = "distserve")

    # TODO: Deprecated
    TP: Optional[int]  # Tensor parallelism (default = 1)
    pass


class Worker:
    def __init__(
        self, env, wid,
        cluster=None,
        is_last_in_pipeline: bool = False,
        pipe_rank: int = None,
        should_request_stay: bool = True,
        prefill_max_batch_size: int = 10 ** 7,
        decode_max_batch_size: int = 10 ** 7,
        global_scheduler: 'Scheduler' = None,
        model_type: str = None,
        TP: int = 1,
        TP_Prefill: int = None,
        TP_Decode: int = None,
        enable_chunked_prefill=False,
        prefill_max_tokens=10 ** 7,
        decode_max_tokens=10 ** 7,
        free_mem_slots_num=69230, # TODO: (Refactor) This is a magic number. Should be a configuration.
        per_token_kvcache_transfertime=0.01, # TODO: (Refactor) This is a magic number. Should be a configuration.
        decode_back_pressure: float = 0.9,
        engine_type: Literal["distserve", "vllm"] = "distserve",
    ):
        self.env = env
        self.cluster = cluster  # Refer to the cluster of init.
        self.wid = wid
        self.pipe_rank = pipe_rank
        self.is_last_in_pipeline = is_last_in_pipeline
        self.next_worker: 'Optional[Worker]' = None
        self.model_type = model_type

        # TODO: (Deprecate) TP should be deprecate in favor of TP_prefill and TP_decode.
        self.TP = TP
        self.TP_Prefill = TP_Prefill
        self.TP_Decode = TP_Decode
        if (self.TP_Prefill is None) and (self.TP_Decode is None):
            warnings.warn(f"TP_Prefill and TP_Decode are not set. Default to {TP = } only apply to prefill.")
            self.TP_Prefill = TP
            self.TP_Decode = 1
        elif (self.TP_Prefill is not None) and (self.TP_Decode is not None):
            # Using the new TP_prefill and TP_decode value, instead of TP.
            pass
        elif (self.TP_Prefill is None) or (self.TP_Decode is None):
            warnings.warn(f"{TP = } will be deprecated soon. Use TP_Prefill and TP_Decode.")
            self.TP_Prefill = TP
            self.TP_Decode = 1
            pass

        # Same request should stay in the same worker.
        # If set to false, then it will forward to the global scheduler.
        self.global_scheduler = global_scheduler
        self.should_request_stay: bool = should_request_stay
        # Maximum number requests to fill in prefill batch. (Default 0 => 10 ** 7, big enough number)
        self.prefill_max_batch_size: int = prefill_max_batch_size if prefill_max_batch_size > 0 else 10 ** 7
        self.decode_max_batch_size: int = decode_max_batch_size if decode_max_batch_size > 0 else 10 ** 7
        # Maximum number of tokens for a prefill request to batch.
        self.prefill_max_tokens: int = prefill_max_tokens if prefill_max_tokens > 0 else 10 ** 7
        self.decode_max_tokens: int = decode_max_tokens if decode_max_tokens > 0 else 10 ** 7
        # Enable chunked prefill (if True) or prioritization scheduling (if False)
        self.enable_chunked_prefill: bool = enable_chunked_prefill
        # Decode worker stop accepting incoming request when this is full.
        self.decode_back_pressure = decode_back_pressure

        self.prefill_queue: 'deque[Request]' = deque()
        self.decode_queue: 'deque[Request]' = deque()
        # Transfer kv-cache to other workers, every request will be push to this queue after prefill,
        # and waitng for some worker to receive it and decode.
        self.migrate_queue: 'deque[Request]' = deque()
        self._prefill_ips: int = 0  # Elements in progress for prefill
        self._decode_ips: int = 0  # Elements in progress for decode
        self._wakeup_event = env.event()
        self.log: 'list[tuple[float, str, int, int, int, list[int], list[int]]]' = []

        self.free_mem_slots_num = free_mem_slots_num # free slots for kv-cache
        self.max_mem_slot_num = free_mem_slots_num
        self.mem_slot_lower_bound = 0.1 * free_mem_slots_num # 10% of free slots
        self.per_token_kvcache_transfertime = per_token_kvcache_transfertime

        # Simulate scheduler delay in terms of number of decode rounds.
        self._prefill_sched_delay: int = 0
        self.engine_type = engine_type
        pass

    @property
    def is_first_in_pipeline(self):
        return self.pipe_rank == 0


    def __repr__(self):
        return f"Worker {self.wid}"

    def _log_event(self, event, num_tokens: int = 0, prefill_bs=0, decode_bs=0,
                   prefill_len_list=None, decode_len_list=None):
        if prefill_len_list is None:
            prefill_len_list = []
        if decode_len_list is None:
            decode_len_list = []
        item = (self.env.now, event, num_tokens, prefill_bs, decode_bs, prefill_len_list, decode_len_list)
        self.log.append(item)
        # print(item)
        return

    def run(self):
        while True:
            self.check_migrate_queue()
            if not (self.prefill_queue or self.decode_queue):
                yield self._wakeup_event

            if self.prefill_queue :
                if self.mem_is_enough():
                    yield from self.do_prefill()
                else:
                    yield self.env.timeout(0.1)  # avoid dead lock
            else:
                yield from self.do_decode()

            self._log_event("wait")
            pass

        pass

    def add_ray_overhead(self, sum_of_tokens) -> int:
        base_overhead = 2
        k = 0.0001
        delay = base_overhead + sum_of_tokens * k
        return delay

    # run = run_with_schedule_delay

    def wakeup(self):
        self._wakeup_event.succeed()
        self._wakeup_event = self.env.event()
        return

    def forward_prefill(self, items):
        # if items is not iterable, then make it iterable
        if not items:
            return
        if not isinstance(items, Iterable):
            items = [items]

        self.next_worker.prefill_queue.extend(items)
        self.next_worker.wakeup()
        return

    def forward_decode(self, items: Union['Request', Iterable['Request']], to_scheduler: bool = False):
        if not items:
            return
        if not isinstance(items, Iterable):
            items = [items]

        if not to_scheduler:
            self.next_worker.decode_queue.extend(items)
            self.next_worker.wakeup()
            return

        for item in items:
            self.global_scheduler.schedule_decode(item)
        return

    def _enter_decodes(self, remaining_tok_in_batch: int) -> 'List[Request]':
        _decode_len = min(remaining_tok_in_batch, len(self.decode_queue))
        decode_reqs: 'List[Request]' = []
    
        # if memory is not enough, only schedule the requests that have been decoded before
        # because their kv-cache is already alloced, new requests' kv-cache cost too much free memory    
        decode_all_kinds_requests = self.mem_is_enough()

        # request is given up if 
        # 1. request needs kv-cache migrate but memory is less than the mem_slot_lower_bound
        # 2. available memory is not enough
        requests_give_up = deque()
        
        if self.mem_is_enough() or decode_all_kinds_requests:
            available_slots = self.free_mem_slots_num // 2 # avoid free_mem_slots_num is used in one batch
        else:
            available_slots = self.free_mem_slots_num // 128 # batch size control
        
        for _ in range(_decode_len):
            left_req = self.decode_queue[0]
            self.decode_queue.popleft()
            if not left_req.kvcache_is_transferred: # requests just finished prefill
                if not decode_all_kinds_requests:
                    requests_give_up.append(left_req) # needs kv-cache migrate, give up
                    continue
                elif available_slots > left_req.current_context_len : # memory is enough, migrate kv-cache
                    available_slots -= left_req.current_context_len
                    decode_reqs.append(left_req)
                else: # memory is not enough, give up the request
                    requests_give_up.append(left_req)
            else: # common requests
                if available_slots <= 1:
                    requests_give_up.append(left_req)
                    continue
                decode_reqs.append(left_req)
                available_slots -= 1


            
        # put the requests kicked back to the queue from front
        while len(requests_give_up) > 0:
            self.decode_queue.appendleft(requests_give_up.pop())
        assert len(self.decode_queue) + len(decode_reqs) == _decode_len
            
        # kv-cache transfer
        migrate_time = 0
        for r in list(decode_reqs):
            if not r.kvcache_is_transferred: 
                if r.prefill_worker.wid != self.wid: # if the kv-cache is not in the worker, then migrate
                    migrate_time += self.per_token_kvcache_transfertime * r.current_context_len
                self.migrate_alloc_kvcache([r,])
                r.prefill_worker.wakeup()
            
        yield self.env.timeout(migrate_time)
        self.decode_alloc_kvcache(decode_reqs)
         
        for r in decode_reqs:
            r.do_decode(wid=self.wid)
        return decode_reqs

    def _enter_prefill(self) -> 'List[Request]':
        result: 'List[Request]' = []

        # check if free_slot_num touches the lower bound
        if not self.mem_is_enough():
            return result
        
        available_slots = self.free_mem_slots_num

        # Limit the maximum prefill requests to handle.
        max_request_size = min(self.prefill_max_batch_size, len(self.prefill_queue))

        # TODO: (Refactor) This logic becomes spaghetti.
        # If worker is not the first in pipeline, then it will just identify the chunks of prefill.
        if not self.is_first_in_pipeline:
            # Then just fetch all decode with the same chunk-id.
            chunk_id = self.prefill_queue[0].chunk_id
            for i in range(max_request_size):
                candidate: 'Request' = self.prefill_queue[0]
                if candidate.chunk_id != chunk_id:
                    break
                if available_slots < candidate.current_prefill_lens: # TODO: not sure if this is correct
                    break
                result.append(self.prefill_queue.popleft())
                available_slots -= candidate.current_prefill_lens
            pass

        else:
            # Worker is the first in pipeline, then it will do chunked prefill.
            chunk_size = 0
            prefill_max_tokens = self.prefill_max_tokens
            # chunk_id assign as uuid
            chunk_id = UUID(int=random.getrandbits(128))
            for _ in range(max_request_size):
                candidate: 'Request' = self.prefill_queue[0]

                if self.enable_chunked_prefill:
                    # The prefill portion that we picked from the candidate.
                    sched_size = min(
                        # The to-schedule size is the minimum of
                        # (1) the remaining prefill size of the candidate, and
                        # (2) the maximum allowed size of a chunked-prefill batch.
                        # This way we greedily cut and schedule the prefill chunk.
                        candidate.remain_prefill_lens,
                        prefill_max_tokens - chunk_size  # max batch size in a chunked-prefill batch - chunk size
                    )
                    if sched_size <= 0:
                        break
                else:
                    # If the whole request can fit into the chunk,
                    # then just schedule the whole request.
                    sched_size = candidate.remain_prefill_lens
                    if sched_size > prefill_max_tokens:
                        break
                    pass

                if available_slots < sched_size:
                    break
                available_slots -= sched_size
                # Candidate is picked. Now fill in the chunked-prefill information.
                candidate.current_prefill_lens = sched_size
                candidate.remain_prefill_lens -= sched_size
                prefill_max_tokens -= sched_size
                candidate.chunk_id = chunk_id
                chunk_size += sched_size
                assert candidate.remain_prefill_lens >= 0
                result.append(self.prefill_queue.popleft())
                pass
        for i in result:
            i.do_prefill(wid=self.wid)
            
        if result:
            self.prefill_alloc_kvcache(result)
            
        return result

    def _exit_prefill(self, prefill_items: List['Request']):
        # if a request finished prefill, it should be migrated to other workers
        requests_need_migrate = [] 
        
        for item in prefill_items:
            next_wid = self.next_worker.wid if self.next_worker else None
            item.finish_prefill(is_finished_one_round=self.is_last_in_pipeline, wid=self.wid, next_wid=next_wid)
            if item.prefill_is_done:
                item.prefill_worker = self
                requests_need_migrate.append(item)
            if not self.is_last_in_pipeline or (item.remain_prefill_lens > 0):
                # Finish one chunk of prefill. Now forward to the next worker
                # (or head of worker) to do the rest of the parts.
                self.forward_prefill(item)
                continue

            # Arrive at worker who is at the last of pipeline.
            if item.should_finish():
                # ... just a sanity check to avoid any infinite loop.
                continue
            self.forward_decode(item, to_scheduler=(not self.should_request_stay))
        self.migrate_kvcache(requests_need_migrate)
        return

    def _exit_decode(self, decode_reqs: 'List[Request]'):
        finished_requests = [] # if the request is finished, its kv-cache should be freed

        if not decode_reqs:
            return
        next_wid = self.next_worker.wid if self.next_worker else None
        for r in decode_reqs:
            r.finish_decode(is_finished_one_round=self.is_last_in_pipeline, next_wid=next_wid)
            if r._terminated:
                finished_requests.append(r)
        next_decode_batch = tuple(r for r in decode_reqs if not r.should_finish())
        self.decode_free_kvcache(finished_requests)
        self.forward_decode(next_decode_batch)
        return

    def do_prefill(self):
        prefill_items: 'List[Request]' = self._enter_prefill()
        if not prefill_items:
            return
        if self.enable_chunked_prefill:
            remaining_tok_in_batch = self.prefill_max_tokens - sum(x.current_prefill_lens for x in prefill_items)
            decode_reqs = self._enter_decodes(remaining_tok_in_batch)
        else:
            decode_reqs = []
        # TODO: (Refactor) The `num_tokens` may be used inaccurately in the get prefill time function.
        num_tokens = sum(x.current_prefill_lens for x in prefill_items)
        num_tokens += len(decode_reqs)

        self._log_event(
            "do_prefill",
            num_tokens=num_tokens,
            prefill_bs=len(prefill_items),
            decode_bs=len(decode_reqs),
            prefill_len_list=[x.current_prefill_lens for x in prefill_items],
            decode_len_list=[x.current_context_len for x in decode_reqs],
        )

        # Get prefill time wrt total number of tokens.
        delay = get_prefill_time(
            num_tokens,
            bs=len(prefill_items),
            decode_bs=len(decode_reqs),
            pp=self.cluster.PP_prefill,
            model_type=self.model_type, TP=self.TP_Prefill,
            prefill_len_list=[x.current_prefill_lens for x in prefill_items],
            engine_type=self.engine_type,
            # __prefill_reqs=prefill_items,
            # __decode_reqs=decode_reqs,
        )
        num_tokens = sum(x.current_context_len for x in (prefill_items + decode_reqs))
        if self.is_first_in_pipeline:
            delay += self.add_ray_overhead(num_tokens)
        # Set the number of prefills in progress such that the scheduler get proper information about the worker.
        self._prefill_ips = len(prefill_items)
        yield self.env.timeout(delay)
        self._prefill_ips = 0
        self._exit_prefill(prefill_items)
        self._exit_decode(decode_reqs)
        return

    def do_decode(self):
        decode_reqs = yield self.env.process(self._enter_decodes(self.decode_max_tokens))
        batch_size = len(list(decode_reqs))
        if batch_size == 0:
            return
        
        self._log_event(
            "do_decode", num_tokens=batch_size, decode_bs=batch_size,
            decode_len_list=[x.current_context_len for x in decode_reqs],
        )
        _token_generated_list = [x.current_context_len + 1 for x in decode_reqs]
        delay = get_decode_time(batch_size, pp=self.cluster.PP_decode,
                                model_type=self.model_type, TP=self.TP_Decode,
                                token_generated_list=_token_generated_list,
                                engine_type=self.engine_type, )
        num_tokens = sum(x.current_context_len for x in decode_reqs)
        if self.is_first_in_pipeline:
            delay += self.add_ray_overhead(num_tokens)
        yield self.env.timeout(delay)
        self._exit_decode(decode_reqs)
        return

    pass

    def check_migrate_queue(self):
        # check if requests' kv-cache is received by other workers, if yes then free the slots
        _migrate_queue_len = len(self.migrate_queue)
        if len(self.migrate_queue) == 0:
            return
        migrated_requests = []
        stay_requests = []
        for i in range(len(self.migrate_queue)):
            r = self.migrate_queue.popleft()
            if r.kvcache_is_transferred:
                migrated_requests.append(r)
            else:
                stay_requests.append(r)
        for r in stay_requests:
            self.migrate_queue.append(r)
        assert len(migrated_requests) + len(stay_requests) == _migrate_queue_len
        if migrated_requests:
            self.prefill_free_kvcache(migrated_requests)

    def mem_is_enough(self, requests: List['Request']=[]) -> bool:
        assert self.free_mem_slots_num <= self.max_mem_slot_num
        assert self.free_mem_slots_num >= 0

        if requests and all(r.counter > 0 for r in requests):
            return True

        return self.free_mem_slots_num >= self.mem_slot_lower_bound


    def migrate_kvcache(self, requests: 'list[Request]') -> None:
        # called by prefill worker, push the requests to the migrate queue, 
        # and waiting for the decode worker to receive it
        # TODO: if the request's output_len == 1, decode won't happen, the kv-cache should be freed
        for i in requests:
            self.migrate_queue.append(i)
            i.wait_kvcache_migration(wid=self.wid)

    def prefill_alloc_kvcache(self, requests: 'list[Request]') -> bool: 
        # allocate slots for kv-cache
        if not self.mem_is_enough():
            return False
        for i in requests:
            i.kvcache_generated = True
        self.free_mem_slots_num -= sum([request.current_prefill_lens for request in requests]) 
        self._log_event('prefill_alloc_kvcache')
        return True


    def prefill_free_kvcache(self, requests: 'list[Request]') -> None: 
        # called by prefill worker, free the slots immediately after kv-cache migration finished
        self.free_mem_slots_num += sum([request.prefill_lens for request in requests]) 
        self._log_event('prefill_free_kvcache')
        return

    def migrate_alloc_kvcache(self, requests: 'list[Request]') -> bool:
        # called by the decode worker, prepare for the kv-cache migration
        assert not any([request.kvcache_is_transferred for request in requests]), "The kv-cache is already transferred."
        self.free_mem_slots_num -= sum([(request.prefill_lens) for request in requests])
        for r in requests:
            r.kvcache_is_transferred = True

    def decode_alloc_kvcache(self, requests: 'list[Request]') -> bool:
        # for each request, decode once cost one slot
        self.free_mem_slots_num -= len(requests)
        self._log_event('decode_alloc_kvcache')
        return True

    def decode_free_kvcache(self, requests: 'list[Request]') -> None:
        # free the slots immediately after decoding finished
        self.free_mem_slots_num += sum([(request.current_context_len) for request in requests])
        self._log_event('decode_free_kvcache')
        return

    def __del__(self):
        assert self.free_mem_slots_num == self.max_mem_slot_num, f"worker:{self.wid} free_mem_slots_num: {self.free_mem_slots_num}, max_mem_slot_num: {self.max_mem_slot_num}"
