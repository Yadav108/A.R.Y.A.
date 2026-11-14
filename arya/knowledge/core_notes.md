# ARYA core notes — mechatronics & AI fundamentals

Reference sheet written for ARYA (by Claude) to fill gaps between course materials.
Standard textbook results only. Your own course material takes priority where it differs
(notation and sign conventions vary between courses) — check the cited source.

## Electrical circuits: Ohm's law, power, Kirchhoff's laws

Ohm's law: V = I · R (voltage in volts, current in amperes, resistance in ohms).
Electrical power: P = V · I = I² · R = V² / R (watts).
Kirchhoff's current law (KCL): the sum of currents entering a node equals the sum leaving it (charge is conserved).
Kirchhoff's voltage law (KVL): the sum of voltages around any closed loop is zero (energy is conserved).
Series resistors: R_total = R1 + R2 + … ; parallel resistors: 1/R_total = 1/R1 + 1/R2 + …
Voltage divider: V_out = V_in · R2 / (R1 + R2). Current divider (two branches): I1 = I · R2 / (R1 + R2).

## Capacitors, inductors and time constants

Capacitor: i = C · dv/dt, stored energy E = ½ C V². Inductor: v = L · di/dt, stored energy E = ½ L I².
RC circuit time constant: τ = R · C. Charging: v(t) = V_s · (1 − e^(−t/τ)); after 1τ ≈ 63.2 %, after 5τ ≈ 99.3 % (treated as fully charged).
RL circuit time constant: τ = L / R. Current rise: i(t) = (V_s/R) · (1 − e^(−t/τ)).
Impedances: resistor Z = R, capacitor Z = 1/(jωC), inductor Z = jωL.
RC low-pass filter cut-off frequency: f_c = 1 / (2π R C); at f_c the gain is −3 dB (≈ 0.707) and the phase is −45°.

## Operational amplifiers (ideal)

Ideal op-amp rules with negative feedback: no current into the inputs, and the two input voltages are equal (virtual short).
Inverting amplifier: V_out = −(R_f / R_in) · V_in.
Non-inverting amplifier: V_out = (1 + R_f / R_g) · V_in.
Voltage follower (buffer): V_out = V_in — high input impedance, low output impedance.
Summing amplifier (inverting): V_out = −R_f · (V1/R1 + V2/R2 + …).
Integrator: V_out = −(1 / (R C)) ∫ V_in dt. Differentiator: V_out = −R C · dV_in/dt.

## DC motor model

Armature circuit: V = R_a · i + L_a · di/dt + e_b, with back-EMF e_b = K_e · ω.
Torque: T = K_t · i (in SI units K_t = K_e).
Mechanical: J · dω/dt = T − b · ω − T_load.
Neglecting inductance, the speed responds like a first-order system: ω(s)/V(s) = K / (τ_m s + 1).
Steady-state no-load speed is roughly proportional to supply voltage; stall torque = K_t · V / R_a.
Mechanical power output: P = T · ω.

## Sensors and measurement basics

Accuracy: closeness to the true value. Precision (repeatability): closeness of repeated readings to each other.
Resolution: smallest detectable change. Sensitivity: change of output per change of input (slope of the characteristic).
Strain gauge: ΔR/R = k · ε, gauge factor k ≈ 2 for metal foil gauges; usually read in a Wheatstone bridge.
Wheatstone bridge balance: R1/R2 = R3/R4. Quarter bridge output (small strain): V_out ≈ V_s · k · ε / 4.
Incremental encoder: resolution = 360° / (pulses per revolution); quadrature decoding (A/B channels) gives 4× counts and direction.
Thermocouples produce a voltage from a temperature difference (Seebeck effect); RTDs (e.g. Pt100: 100 Ω at 0 °C) change resistance with temperature.
Measurement uncertainty from independent sources combines as the root sum of squares: u = sqrt(u1² + u2² + …).

## Sampling, ADCs and signals

Nyquist–Shannon: sample at more than twice the highest signal frequency (f_s > 2 f_max) to avoid aliasing; use an anti-aliasing low-pass filter before the ADC.
ADC resolution: an n-bit ADC has 2^n levels; LSB size = V_ref / 2^n. Quantisation error ≤ ±½ LSB.
Signal-to-noise ratio of an ideal n-bit ADC ≈ 6.02 n + 1.76 dB.
Decibels: power ratio dB = 10 log10(P2/P1); amplitude ratio dB = 20 log10(A2/A1).
Fourier idea: any periodic signal is a sum of sinusoids at multiples of its fundamental frequency.

## Dynamics: Newton–Euler, energy, momentum

Newton's second law: F = m · a (translation); M = J · α (rotation about a fixed axis or the centre of mass).
Kinetic energy: T = ½ m v² + ½ J ω². Potential energy: gravity V = m g h, spring V = ½ k x².
Work–energy theorem: work done by all forces = change in kinetic energy.
Linear momentum p = m v; impulse ∫F dt = Δp. Angular momentum about the centre of mass: L = J ω.
D'Alembert's principle: treat −m·a (and −J·α) as inertial forces, then solve as statics.
Moments of inertia about the centre: slender rod (length l) J = m l² / 12; solid disc/cylinder J = m r² / 2; thin ring J = m r²; solid sphere J = 2 m r² / 5.
Parallel-axis theorem: J_A = J_S + m d², where d is the distance between axis A and the parallel axis through the centre of mass S.

## Kinematics of rigid bodies and relative motion

Velocity of a point on a rigid body: v_P = v_A + ω × r_AP.
Acceleration: a_P = a_A + α × r_AP + ω × (ω × r_AP) — the last term is the centripetal part, pointing toward the rotation axis.
Rolling without slipping: v_centre = ω r and a_centre = α r.
Motion relative to a rotating frame (angular velocity Ω): a_abs = a_rel + a_frame + 2 Ω × v_rel, where 2 Ω × v_rel is the Coriolis acceleration.
Instantaneous centre of rotation: the point with zero velocity at that instant; every velocity is perpendicular to the line from it.

## Vibrations: mass–spring–damper

Equation of motion: m ẍ + c ẋ + k x = F(t).
Natural frequency ω_n = sqrt(k/m) (rad/s); f_n = ω_n / (2π) (Hz).
Damping ratio ζ = c / (2 sqrt(k m)); critical damping c_crit = 2 sqrt(k m).
ζ < 1 under-damped (oscillates), ζ = 1 critically damped (fastest without overshoot), ζ > 1 over-damped.
Damped natural frequency ω_d = ω_n sqrt(1 − ζ²).
Logarithmic decrement δ = ln(x_n / x_(n+1)) = 2π ζ / sqrt(1 − ζ²).
Resonance: forcing near ω_n gives large amplitudes; the peak height grows as damping decreases.

## Laplace transforms and transfer functions

Transfer function G(s) = Y(s)/U(s) with zero initial conditions. Differentiation becomes multiplication by s.
Common pairs: unit step 1/s; ramp 1/s²; e^(−a t) ↔ 1/(s + a); sin(ω t) ↔ ω/(s² + ω²); cos(ω t) ↔ s/(s² + ω²).
Final value theorem: y(∞) = lim(s→0) s · Y(s), valid only if the system is stable. Steady-state value of a step response = G(0).
Poles: roots of the denominator; they decide stability and speed. Zeros: roots of the numerator.
A continuous system is stable if all poles have negative real parts (left half of the s-plane).
Series blocks multiply: G = G1 · G2. Parallel blocks add: G = G1 + G2.
Closed loop with negative feedback: T(s) = G(s) / (1 + G(s) H(s)).

## First- and second-order system responses

First order: G(s) = K / (τ s + 1). Step response y(t) = K (1 − e^(−t/τ)); 63.2 % at t = τ; 2 % settling ≈ 4τ; rise time (10–90 %) ≈ 2.2 τ.
Second order: G(s) = K ω_n² / (s² + 2 ζ ω_n s + ω_n²). Its two parameters are the damping ratio ζ and the natural frequency ω_n.
Stability of a second-order system: stable when both poles have negative real parts, which means ζ > 0 and ω_n > 0 (all coefficients of s² + 2ζω_n s + ω_n² positive). Stability does NOT require ζ < 1: overdamped systems are stable too. ζ = 0 is marginally stable; ζ < 0 is unstable (the response grows). Overshoot and settling time describe performance, not stability.
Damping cases: ζ = 0 undamped (oscillates forever at ω_n, never settles); 0 < ζ < 1 underdamped (decaying oscillation with overshoot); ζ = 1 critically damped (fastest response without overshoot); ζ > 1 overdamped (no oscillation, slower). Damped frequency ω_d = ω_n · sqrt(1 − ζ²).
ω_n sets the speed: a larger ω_n gives a faster response (same shape, shorter time scale).
Percent overshoot: %OS = 100 · e^(−ζπ / sqrt(1 − ζ²)) for 0 < ζ < 1 (e.g. ζ = 0.3 gives about 37 %, ζ = 0.7 about 4.6 %).
Peak time t_p = π / ω_d. 2 % settling time t_s ≈ 4 / (ζ ω_n). Poles at s = −ζ ω_n ± j ω_d.

## PID control

u(t) = K_p e(t) + K_i ∫ e dt + K_d de/dt; transfer function C(s) = K_p + K_i/s + K_d s.
P: faster response, but a steady-state error usually remains for type-0 plants.
I: removes steady-state error for step inputs, but adds phase lag, overshoot and risk of windup (use anti-windup / clamping).
D: adds damping and reduces overshoot, but amplifies noise — filter it in practice (K_d s / (T_f s + 1)).
Ziegler–Nichols (ultimate gain method): find K_u and oscillation period T_u, then K_p = 0.6 K_u, T_i = T_u / 2, T_d = T_u / 8.
Steady-state error for a unit step with unity feedback: e_ss = 1 / (1 + K_pos), where the position constant K_pos = lim(s→0) G(s). It is finite and non-zero for a type-0 open loop (no pole at s = 0) and zero for type 1 or higher (at least one integrator).

## Stability and frequency response

Routh–Hurwitz: all characteristic-equation coefficients must be positive and the first column of the Routh array must have no sign changes (each sign change = one right-half-plane pole).
Bode plot: magnitude in dB and phase versus log frequency. A pole adds −20 dB/decade and −90°; a zero adds +20 dB/decade and +90°.
Gain margin: how much gain can increase before instability (measured where the phase is −180°).
Phase margin: extra phase lag tolerated at the gain-crossover frequency (where |G| = 0 dB). Typical design target: 45°–60°.
Bandwidth: frequency where the closed-loop gain drops 3 dB below its low-frequency value.

## State-space models

ẋ = A x + B u, y = C x + D u. Poles = eigenvalues of A.
Transfer function: G(s) = C (sI − A)^(−1) B + D.
Controllability: rank [B, AB, …, A^(n−1)B] = n. Observability: rank [C; CA; …; CA^(n−1)] = n.
State feedback u = −K x places the closed-loop poles at the eigenvalues of A − B K (possible for any locations if controllable).

## Robot kinematics

Forward kinematics: joint angles → end-effector pose. Inverse kinematics: desired pose → joint angles (may have several or no solutions).
Two-link planar arm: x = l1 cos θ1 + l2 cos(θ1 + θ2), y = l1 sin θ1 + l2 sin(θ1 + θ2).
Denavit–Hartenberg (classic) parameters per joint: θ (joint angle), d (link offset), a (link length), α (link twist); each joint gives a homogeneous transform, and chaining them gives the end-effector pose.
Jacobian J(q): maps joint velocities to end-effector velocity, ẋ = J(q) q̇. Singularities are where J loses rank (the arm loses a direction of motion).
Static forces: joint torques τ = Jᵀ F.

## Digital logic and PLCs

Boolean algebra: AND (·), OR (+), NOT (¯). De Morgan: ¬(A·B) = ¬A + ¬B and ¬(A + B) = ¬A · ¬B.
NAND and NOR are each universal (any logic function can be built from them alone).
Flip-flops store one bit; an SR latch has set/reset inputs, a D flip-flop copies D on the clock edge.
PLC scan cycle: read inputs → execute the program → write outputs → housekeeping, repeated continuously.
IEC 61131-3 languages: Ladder Diagram (LD), Function Block Diagram (FBD), Structured Text (ST), Instruction List (IL, deprecated) and Sequential Function Chart (SFC).
Common function blocks: TON (on-delay timer), TOF (off-delay timer), CTU/CTD (up/down counters), R_TRIG/F_TRIG (edge detection).
Normally open (NO) contacts close when the input is true; normally closed (NC) contacts open. Safety stop circuits are typically wired NC so a broken wire stops the machine.

## Machine learning basics

Supervised learning: learn a mapping from inputs to labelled outputs (regression for numbers, classification for categories).
Unsupervised learning: find structure without labels (clustering such as k-means, dimensionality reduction such as PCA).
Reinforcement learning: an agent learns actions that maximise cumulative reward through trial and error.
Train / validation / test split: fit on training data, tune hyperparameters on validation, report final performance once on the test set.
Overfitting: low training error but high test error (model memorises noise). Remedies: more data, regularisation, simpler models, early stopping, dropout.
Bias–variance trade-off: too simple → high bias (underfitting); too complex → high variance (overfitting).

## Linear and logistic regression, gradient descent

Linear regression: ŷ = wᵀx + b, minimising mean squared error MSE = (1/n) Σ (y − ŷ)².
Logistic regression: p = σ(wᵀx + b) with sigmoid σ(z) = 1 / (1 + e^(−z)); trained with cross-entropy loss.
Gradient descent: w ← w − η ∇L(w), with learning rate η. Too large η diverges or oscillates; too small is slow.
Stochastic / mini-batch gradient descent uses a random subset of data per step; Adam adapts the step size per parameter.
Regularisation: L2 (ridge) adds λ Σ w² and shrinks weights; L1 (lasso) adds λ Σ |w| and can set weights to exactly zero.

## Neural networks

A neuron computes a = f(wᵀx + b) with an activation function f.
Activations: ReLU f(z) = max(0, z) (default for hidden layers); sigmoid for probabilities in (0, 1); tanh in (−1, 1); softmax turns a vector into class probabilities summing to 1.
Backpropagation: applies the chain rule layer by layer to get the gradient of the loss with respect to every weight; then gradient descent updates them.
Vanishing gradients: gradients shrink through many layers (worse with sigmoid/tanh); ReLU, residual connections and normalisation help.
Convolutional neural networks (CNNs): learn local filters shared across an image; pooling reduces resolution; suited to images and spatial signals.
Recurrent networks (RNN, LSTM, GRU): process sequences step by step with a hidden state; LSTMs/GRUs use gates to keep long-term information.
Transformers: use self-attention so every token can weigh every other token; the basis of modern language models (like ARYA's brain).

## Evaluating models

Classification: accuracy = correct / total; precision = TP / (TP + FP); recall = TP / (TP + FN); F1 = 2 · precision · recall / (precision + recall).
Confusion matrix: counts of true/false positives and negatives. Accuracy is misleading on imbalanced classes — prefer precision, recall, F1 or ROC-AUC.
Regression: MAE (mean absolute error), MSE / RMSE, and R² (fraction of variance explained).
Cross-validation (k-fold): train k times, each time holding out a different fold, and average the scores.

## AI in mechatronics

Typical uses: predictive maintenance (detecting anomalies in vibration or current signals), computer vision for inspection and robot guidance, sensor fusion, and learning-based control.
Kalman filter: optimally combines a model prediction with noisy measurements (predict → update), weighting each by its uncertainty; the extended Kalman filter handles mildly nonlinear systems.
Classical control (PID, state feedback) is predictable and certifiable; learned controllers can handle complex behaviour but need careful validation and safety limits.
Edge AI: running models on the device itself (low latency, works offline, keeps data private) — which is how ARYA runs on this laptop.
