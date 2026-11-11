# Tutorial 2: rotated gate sweeps with composite channels

**You will learn:** composite channels, which let you sweep physical quantities instead of
instrument voltages, and setting several of them together.

**The physics.** Your bilayer graphene has a top gate too. The two gates together set
two physical quantities:

* the **carrier density** $n = (C_b V_{bg} + C_t V_{tg})/e + n_0$;
* the **displacement field** $D = (C_b V_{bg} - C_t V_{tg})/2\varepsilon_0$, which
  opens a band gap in bilayer graphene.

To see the gap open, you want to sweep $n$ at fixed $D$, then step $D$. In the
$(V_{bg}, V_{tg})$ plane these are lines at an angle, a **rotated sweep**: both gates
move at every point. easyMeasure does this with a **composite channel**. You define $n$
and $D$ as channels of their own, give the formulas for the gate voltages, and then
sweep $n$ and $D$ like any other channel.

For this device, $C_b/e = 0.6$ and $C_t/e = 0.9 \times 10^{12}\,\text{cm}^{-2}\,\text{V}^{-1}$,
$n_0 = -0.15 \times 10^{12}\,\text{cm}^{-2}$, and $e/2\varepsilon_0 = 0.0452$ V/nm per
$10^{12}\,\text{cm}^{-2}$. Solving the two equations for the gate voltages
($n$ in $10^{12}\,\text{cm}^{-2}$, $D$ in V/nm):

$$
V_{bg} = \frac{n + 0.15 + D/0.0452}{2 \times 0.6}, \qquad
V_{tg} = \frac{n + 0.15 - D/0.0452}{2 \times 0.9}
$$

## 1. The top gate

Add a second `sim_smu` named `tg` (address `NONE`). Make an alias of `tg.voltage`
named `Vtg`, with limits −10 … 10 V and ramp rate 5 V/s, exactly like `Vbg`
([Quick start, step 4](../quickstart.md#4-name-the-channels-you-care-about)).

## 2. The composite channel

On *Instruments & channels*, under **Composites**, click **Add…**:

![A multivariate composite: n and D drive Vbg and Vtg](../images/t2-composite.png)

1. **Type**: *Multivariate write*. You set variables, and the hardware follows.
2. **Variables**: one row each, using *Add variable*.

    | Name | Min | Max | Unit | Description |
    |---|---|---|---|---|
    | `n` | -2.5 | 2.5 | 1e12 cm^-2 | carrier density |
    | `D` | -0.35 | 0.35 | V/nm | displacement field |

    Each variable gets its own limits, unit and, optionally, a ramp rate.
3. **Targets**: one line per gate, `channel = formula of the variables`:

    ```
    Vbg = (n + 0.15 + D/0.0452) / (2*0.6)
    Vtg = (n + 0.15 - D/0.0452) / (2*0.9)
    ```

4. **Check** asks the backend whether everything is right without saving anything:
   the names, the formulas, and that the targets exist and can be set. It answers
   `✔ defines n, D`.

**OK**, and the composite appears in the list, with the last values of its
variables:

![The composites table](../images/t2-composites.png)

`n` and `D` are now channels like `Vbg`: you can set them, sweep them and record them.
Whenever one of them is set:

* both gate voltages are computed;
* everything is checked against **all** limits: the limits of `n` and `D`, and also
  the ±10 V of `Vbg` and `Vtg`;
* both gates then ramp **together**, each within its own ramp rate.

A point that would push a gate beyond 10 V is refused before anything moves.

## 3. Set n and D by hand

The **Manual** panel now has a group **nD**, with `n` and `D`. There is one catch the
first time. Setting `n` alone needs a value for `D` to compute the gates, and `D` has
never been set. So the first time, set them together:

![Setting n and D together: hold, then Set all](../images/t2-manual.png)

1. Tick **hold**: changes are collected instead of sent at once.
2. Type the values of `n` and `D`.
3. **Set all**: both are set in one go.

From then on, you can change either one alone; the other keeps its value.

## 4. Sweep n and D

In the **Sweep editor**, sweep the *variables*:

![Sweeping n (dimension 1) at each D (dimension 2)](../images/t2-sweep.png)

1. Two dimensions.
2. Dimension 1: `n` from −2.5 to 2.5 in 101 points, snake scan. Dimension 2: `D` from
   −0.35 to 0.35 in 29 points.
3. Record `Rxx`.

**Validate** computes all 2929 points, converts each to gate voltages and checks them.
Try making the range of `n` larger: validation then tells you which point would need a
gate beyond 10 V, and the sweep can't be started. This sweep is
`examples/graphene_nD.json`.

## 5. The result

![Rxx against n and D: the gap opens at n = 0](../images/t2-map.png)

At $n = 0$ the resistance grows steeply with $|D|$: the band gap of bilayer graphene
opening in a displacement field. The same measurement plotted against $V_{bg}$ and
$V_{tg}$ would show the same feature along a tilted line.

!!! tip "Sweeping two things in one dimension"
    You can also put **both** `n` and `D` in the same dimension, e.g. `n` from −2 to 2
    and `D` from −0.3 to 0.3 together. That is a diagonal cut through the $(n, D)$
    plane. Variables of one composite swept in the same dimension are set together at
    every point. With a formula for each, the path can be any curve, such as a circle
    around $n = D = 0$: see [Custom sweeps](../scripting/custom-sweeps.md#a-path-through-the-n-d-plane).

## Other kinds of composite channel

| type | what it does | example |
|---|---|---|
| **Multivariate write** | variables of your choice drive hardware channels through formulas | $n, D$ → $V_{bg}, V_{tg}$; detuning ε and energy U of a double dot |
| **Composite read** | a read-only formula of other channels | conductance `1/Rxx`; $R = V/I$; a sum of two signals |
| **Simple mapping** | a one-to-one view of one channel, with formulas both ways (checked to be each other's inverse) | a field in mT on a supply in A; a temperature in °C on a controller in K |

Formulas can be piecewise: `piecewise(x < 0, -x, 2*x)`, `a if c else b`, `interp(x,
[...], [...])`. Composite channels can also build on each other. See
[Composite channels](../guide/composites.md) for everything.

## What you learned

* A **multivariate composite** turns physical quantities ($n$, $D$) into the voltages
  of several instruments. You sweep the physics; easyMeasure moves the hardware.
* Every point is checked against the limits of the variables **and** of every gate.
* The first time, set the variables together: **hold** + **Set all**.

**Next:** [Tutorial 3: stop at a threshold](stop-at-threshold.md).
