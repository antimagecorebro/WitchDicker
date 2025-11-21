import os
from typing import Dict, List, Tuple, Optional
from controller import IntersectionController

DEBUG = False
STRATEGY = 'demand_based_ultimate'  # Лучший скор


class ParticipantController(IntersectionController):
    """
    Лучшая оптимизированная стратегия управления светофорами.
    Основные фичи:
    - приоритет фаз по эффективности
    - штраф за переключение фазы
    - удержание выгодной фазы (stay-on-phase rule)
    - адаптивная длительность зелёного
    - плавные изменения без дерганий
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.min_green_duration = 7.0
        self.max_green_duration = 70.0

        self.green_phases: Dict[str, List[int]] = {}
        self.cursor: Dict[str, int] = {}
        self.last_phase_actions: Dict[str, Dict[int, float]] = {}

        # Инициализация фаз
        for tls_id in self.tls_ids:
            phases = self.get_phase_catalog(tls_id)
            greens = [
                phase.index for phase in phases
                if "y" not in phase.state and any(ch in ("G", "g") for ch in phase.state)
            ]

            if not greens:
                raise RuntimeError(f"В программе светофора '{tls_id}' нет зелёных фаз.")

            greens.sort()
            self.green_phases[tls_id] = greens
            self.cursor[tls_id] = 0

        # История действий
        for tls_id in self.tls_ids:
            self.last_phase_actions[tls_id] = {}

    # ----------------------------------------------------------------------
    #  Описание трафика и эффективности
    # ----------------------------------------------------------------------

    def _calculate_waiting_traffic(self, observation: Dict, tls_id: str, phase_id: int) -> float:
        waiting_vehicles = observation.get("waiting_vehicles", {}).get(tls_id, {})
        return waiting_vehicles.get(phase_id, 0)

    def _estimate_phase_capacity(self, observation: Dict, tls_id: str, phase_id: int) -> float:
        waiting = self._calculate_waiting_traffic(observation, tls_id, phase_id)
        base_capacity = 100.0
        return max(10.0, base_capacity + waiting * 1.5)

    def _calculate_phase_efficiency(self, observation: Dict, tls_id: str, phase_id: int) -> float:
        waiting = self._calculate_waiting_traffic(observation, tls_id, phase_id)
        capacity = self._estimate_phase_capacity(observation, tls_id, phase_id)

        if waiting <= 0:
            return 0.0

        return waiting * capacity

    def _get_phase_priority(self, observation: Dict, tls_id: str) -> List[Tuple[int, float]]:
        phases = self.green_phases[tls_id]
        scores = []
        for phase_id in phases:
            eff = self._calculate_phase_efficiency(observation, tls_id, phase_id)
            scores.append((phase_id, eff))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores

    # ----------------------------------------------------------------------
    #  Основной алгоритм
    # ----------------------------------------------------------------------

    def decide_next_phase(self, observation: Dict) -> Optional[Dict[str, Dict[str, float]]]:

        decision = {}

        for tls_id in self.tls_ids:

            light_info = observation.get("lights", {}).get(tls_id)
            if not light_info:
                continue

            time_to_switch = light_info.get("time_to_next_switch", 0.0)
            current_phase = light_info.get("current_phase")
            time_in_phase = light_info.get("time_in_phase", 0.0)

            # Не вмешиваемся, если скоро смена
            if time_to_switch > 0.5:
                continue

            # 1. Считаем эффективность фаз
            raw_priorities = self._get_phase_priority(observation, tls_id)

            # 2. Добавляем штраф за переключение
            priorities = []
            for phase_id, eff in raw_priorities:
                if phase_id != current_phase:
                    switch_penalty = 120.0 / (time_in_phase + 1.0)
                    eff -= switch_penalty
                priorities.append((phase_id, eff))

            priorities.sort(key=lambda x: x[1], reverse=True)

            best_phase, _ = priorities[0]

            # 3. Условие "остаться на текущей, если она почти такая же хорошая"
            if current_phase is not None:
                current_eff = next((s for p, s in raw_priorities if p == current_phase), 0)
                best_eff = raw_priorities[0][1]

                if current_eff >= 0.85 * best_eff:
                    best_phase = current_phase

            # 4. Рассчитываем длительность фазы
            waiting = self._calculate_waiting_traffic(observation, tls_id, best_phase)
            capacity = self._estimate_phase_capacity(observation, tls_id, best_phase)

            alpha = 0.27
            beta = 0.17

            duration = self.min_green_duration + alpha * waiting + beta * (capacity ** 0.5)
            duration = max(self.min_green_duration, min(self.max_green_duration, duration))

            # Немного уменьшаем длительность, если фаза идёт слишком долго
            if time_in_phase > 60:
                duration *= 0.9

            # 5. Обновляем историю
            self.last_phase_actions[tls_id][best_phase] = (
                self.last_phase_actions[tls_id].get(best_phase, 0) + 1
            )

            # 6. Формируем действие
            decision[tls_id] = {
                "phase_id": best_phase,
                "duration": float(f"{duration:.1f}")
            }

        return decision or None
