# WH Controller — Fix turn-off logic + overload coordination

## Context
Bugs trouvés via logs HA (2026-05-25 / 2026-05-26):
1. La prise `switch.smart_plug_mini` a un auto-off à 4h. À 13h41 allumage → 17h41 auto-off → 17h41:20 le contrôleur voit OFF, arme sustain 30s sur export=2089W ; 35s plus tard l'export est tombé à 160W mais le contrôleur rallume quand même (il ne re-vérifie pas la condition à l'allumage).
2. Une fois allumé, `_evaluate_heating` n'a aucun stop sur perte de surplus / import ; il attend juste SoC < threshold-10%. Donc reste allumé pendant des heures même quand on importe massivement.
3. Pas de log sur les transitions ON↔OFF externes → débogage difficile.

## Logique cible
- **Démarrage** : conditions de surplus (rule1 OU rule2) soutenues pendant `sustain_seconds` (30s). Re-validation au moment du fire.
- **Engagement minimum** : `min_duration_s` (15 min) une fois allumé — pas d'arrêt automatique avant.
- **Après min_duration** : si les conditions de démarrage ne sont plus vraies, soutenu pendant `sustain_seconds` → coupure.
- **Overload safety** (cons ≥ 7kW + import) : actif y compris pendant min_duration, mais en coordination avec l'EV charger.
- **Cooldown post auto-off** : si on observe une transition ON→OFF qu'on n'a pas commandée, bloquer le rallumage pendant 15 min.

## Tâches

- [ ] **1. Re-validation des conditions à l'allumage** dans `_evaluate_idle` ([water_heater_controller.py:211](custom_components/beem_ai/water_heater_controller.py:211)). Si rule1/rule2 n'est plus vrai au tick de fire, reset le timer au lieu d'allumer.

- [ ] **2. Stop symétrique dans `_evaluate_heating`** après min_duration :
  - Tracker `_stop_armed_since` quand rule1 AND rule2 deviennent False.
  - Si `_seconds_since_turned_on() >= min_duration_s` ET aucune des deux rules vraie pendant `sustain_seconds` → couper.
  - Reset l'arm quand une rule redevient vraie.

- [ ] **3. Détection transitions externes** :
  - Stocker `_last_observed_switch_state` à chaque `evaluate()`.
  - Si on observe ON→OFF sans avoir appelé `_turn_off()` au tick précédent → logger "switch turned off externally" + démarrer cooldown.
  - Si on observe OFF→ON sans avoir appelé `_turn_on()` → logger "switch turned on externally".

- [ ] **4. Cooldown post-extinction externe** (`COOLDOWN_AFTER_EXTERNAL_OFF_S = 15*60`) :
  - Pendant le cooldown, `_evaluate_idle` ne déclenche pas (mais log debug).
  - Cooldown effacé si `mode=Disabled` puis remis sur Auto (réarm explicite via select).

- [ ] **5. Overload coordonné avec EV charger** (dans le coordinator, pas dans les controllers) :
  - Quand cons ≥ 7kW + import détecté dans `_evaluate_surplus_diverters` :
    - Si EV charger en charge : calculer amps cible = (cons - 6500) // 230 amps de réduction → nouveau target_amps = current - reduction.
    - Si target_amps ≥ MIN_CHARGE_AMPS (6) : appeler `ev_charger._set_amps(target)`, marquer `_overload_throttle_since = now`.
    - Sinon : couper l'EV immédiatement.
    - Si après 15s la surcharge persiste : appeler `wh_controller._turn_off()` (forcer, bypasser min_duration).
  - Retirer le check overload "naïf" dans `_evaluate_heating` (le coordinator s'en charge).

- [ ] **6. Tests** :
  - test rallumage bloqué quand surplus s'effondre pendant la fenêtre de sustain.
  - test min_duration empêche l'arrêt avant 15 min même si surplus disparaît.
  - test arrêt symétrique : après 15 min + 30s sans surplus → off.
  - test cooldown : ON externe→OFF externe → IDLE ne ré-allume pas pendant 15 min.
  - test overload : EV throttle avant coupe ; coupe WH après 15s persistant.

## Review

### Fichiers modifiés
- `custom_components/beem_ai/water_heater_controller.py` — nouveau `COOLDOWN_AFTER_EXTERNAL_OFF_S`, détection de transitions externes au début de `evaluate()`, cooldown dans `_evaluate_idle`, refonte de `_evaluate_heating` avec stop symétrique sur perte de surplus après min_duration, suppression de l'overload local (déplacé au coordinator), nouvelle méthode publique `force_stop_overload()`.
- `custom_components/beem_ai/ev_charger_controller.py` — overload réduit d'un coup à `OVERLOAD_TARGET_W` (6500W) au lieu de -1A/tick. Si la réduction ferait passer sous 6A, coupe.
- `custom_components/beem_ai/coordinator.py` — nouvelle `_handle_overload()` appelée avant les évaluations. Track `_overload_started_at`. Après 15s d'overload persistant + WH heating → `force_stop_overload`.
- `tests/test_water_heater_controller.py` — adapté à la nouvelle séparation des responsabilités. Tests ajoutés : cooldown post-OFF externe, stop symétrique, disarm, Disabled vide le cooldown, `force_stop_overload`.
- `tests/test_ev_charger_controller.py` — adapté à la réduction one-shot.
- `tests/test_overload_coordination.py` — nouveau fichier, 7 tests sur la coordination.

### Résultats tests
`373 passed in 1.40s` (366 avant + 7 nouveaux).

### Comportements clés vérifiés
- Plus de boucle de rallumage après auto-off de la prise (cooldown 15 min).
- Pas d'arrêt avant 15 min d'engagement (sauf overload coordonné).
- Après 15 min, stop si surplus disparu pendant 30s consécutives.
- Overload : EV trime d'abord ; si toujours >7kW après 15s, WH coupé.
- Transitions externes loguées en `WARNING`.

