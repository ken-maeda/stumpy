# STUMPY
# Copyright 2019 TD Ameritrade. Released under the terms of the 3-Clause BSD license.
# STUMPY is a trademark of TD Ameritrade IP Company, Inc. All rights reserved.

import numpy as np
from . import core, stump, config
from .aampi import aampi


@core.non_normalized(aampi)
class stumpi:
    """
    Compute an incremental z-normalized matrix profile for streaming data

    This is based on the on-line STOMPI and STAMPI algorithms.

    Parameters
    ----------
    T : numpy.ndarray
        The time series or sequence for which the matrix profile and matrix profile
        indices will be returned

    m : int
        Window size

    egress : bool, default True
        If set to `True`, the oldest data point in the time series is removed and
        the time series length remains constant rather than forever increasing

    normalize : bool, default True
        When set to `True`, this z-normalizes subsequences prior to computing distances.
        Otherwise, this class gets re-routed to its complementary non-normalized
        equivalent set in the `@core.non_normalized` class decorator.

    p : float, default 2.0
        The p-norm to apply for computing the Minkowski distance. This parameter is
        ignored when `normalize == True`.

    k : int, default 1
        The number of top `k` smallest distances used to construct the matrix profile.
        Note that this will increase the total computational time and memory usage
        when k > 1.

    Attributes
    ----------
    P_ : numpy.ndarray
        The updated (top-k) matrix profile for `T`. When `k=1` (default), the first
        (and only) column in this 2D array consists of the matrix profile. When
        `k > 1`, the output has exactly `k` columns consisting of the top-k matrix
        profile.

    I_ : numpy.ndarray
        The updated (top-k) matrix profile indices for `T`. When `k=1` (default),
        the first (and only) column in this 2D array consists of the matrix profile
        indices. When `k > 1`, the output has exactly `k` columns consisting of the
        top-k matrix profile indices.

    left_P_ : numpy.ndarray
        The updated left (top-1) matrix profile for `T`

    left_I_ : numpy.ndarray
        The updated left (top-1) matrix profile indices for `T`

    T_ : numpy.ndarray
        The updated time series or sequence for which the matrix profile and matrix
        profile indices are computed

    Methods
    -------
    update(t)
        Append a single new data point, `t`, to the time series, `T`, and update the
        matrix profile

    Notes
    -----
    `DOI: 10.1007/s10618-017-0519-9 \
    <https://www.cs.ucr.edu/~eamonn/MP_journal.pdf>`__

    See Table V

    Note that line 11 is missing an important `sqrt` operation!

    Examples
    --------
    >>> stream = stumpy.stumpi(
    ...     np.array([584., -11., 23., 79., 1001., 0.]),
    ...     m=3)
    >>> stream.update(-19.0)
    >>> stream.left_P_
    array([       inf, 3.00009263, 2.69407392, 3.05656417])
    >>> stream.left_I_
    array([-1,  0,  1,  2])
    """

    def __init__(self, T, m, egress=True, normalize=True, p=2.0, k=1):
        """
        Initialize the `stumpi` object

        Parameters
        ----------
        T : numpy.ndarray
            The time series or sequence for which the matrix profile and matrix profile
            indices will be returned

        m : int
            Window size

        egress : bool, default True
            If set to `True`, the oldest data point in the time series is removed and
            the time series length remains constant rather than forever increasing

        normalize : bool, default True
            When set to `True`, this z-normalizes subsequences prior to computing
            distances. Otherwise, this class gets re-routed to its complementary
            non-normalized equivalent set in the `@core.non_normalized` class decorator.

        p : float, default 2.0
            The p-norm to apply for computing the Minkowski distance. This parameter is
            ignored when `normalize == True`.

        k : int, default 1
            The number of top `k` smallest distances used to construct the matrix
            profile. Note that this will increase the total computational time and
            memory usage when `k > 1`.
        """
        self._T = core._preprocess(T)
        core.check_window_size(m, max_size=self._T.shape[-1])
        self._m = m
        self._k = k

        self._n = self._T.shape[0]
        self._excl_zone = int(np.ceil(self._m / config.STUMPY_EXCL_ZONE_DENOM))
        self._T_isfinite = np.isfinite(self._T)
        self._egress = egress

        mp = stump(self._T, self._m, k=self._k)
        self._P = mp[:, : self._k].astype(np.float64)
        self._I = mp[:, self._k : 2 * self._k].astype(np.int64)

        self._left_I = mp[:, 2 * self._k].astype(np.int64)
        self._left_P = np.full_like(self._left_I, np.inf, dtype=np.float64)

        self._T, self._M_T, self._Σ_T = core.preprocess(self._T, self._m)
        # Retrieve the left matrix profile values

        # Since each (top-1) matrix profile value is the minimum between the left
        # and right matrix profile values, we can save time by re-computing only
        # the left matrix profile value when the (top-1) matrix profile index is
        # equal to the right matrix profile index.
        mask = self._left_I == self._I[:, 0]
        self._left_P[mask] = self._P[mask, 0]

        # Only re-compute the `i`-th left matrix profile value, `self._left_P[i]`,
        # when `self._left_I[i] != self._I[i, 0]`
        for i in np.flatnonzero(self._left_I >= 0 & ~mask):
            j = self._left_I[i]
            QT = np.dot(self._T[i : i + self._m], self._T[j : j + self._m])
            D_square = core._calculate_squared_distance(
                self._m,
                QT,
                self._M_T[i],
                self._Σ_T[i],
                self._M_T[j],
                self._Σ_T[j],
            )
            self._left_P[i] = np.sqrt(D_square)

        Q = self._T[-self._m :]
        self._QT = core.sliding_dot_product(Q, self._T)
        if self._egress:
            self._QT_new = np.empty(self._QT.shape[0], dtype=np.float64)
            self._n_appended = 0

    def update(self, t):
        """
        Append a single new data point, `t`, to the existing time series `T` and update
        the (top-k) matrix profile and matrix profile indices.

        Parameters
        ----------
        t : float
            A single new data point to be appended to `T`

        Notes
        -----
        `DOI: 10.1007/s10618-017-0519-9 \
        <https://www.cs.ucr.edu/~eamonn/MP_journal.pdf>`__

        See Table V

        Note that line 11 is missing an important `sqrt` operation!
        """
        if self._egress:
            self._update_egress(t)
        else:
            self._update(t)

    def _update_egress(self, t):
        """
        Ingress a new data point, egress the oldest data point, and update the (top-k)
        matrix profile and matrix profile indices

        Parameters
        ----------
        t : float
            A single new data point to be appended to `T`
        """
        self._n = self._T.shape[0]
        l = self._n - self._m + 1 - 1  # Subtract 1 due to egress
        self._T[:-1] = self._T[1:]
        self._T[-1] = t
        self._n_appended += 1
        self._QT[:-1] = self._QT[1:]
        S = self._T[l:]
        t_drop = self._T[l - 1]
        self._T_isfinite[:-1] = self._T_isfinite[1:]

        self._I[:-1] = self._I[1:]
        self._P[:-1] = self._P[1:]
        self._left_I[:-1] = self._left_I[1:]
        self._left_P[:-1] = self._left_P[1:]

        if np.isfinite(t):
            self._T_isfinite[-1] = True
        else:
            self._T_isfinite[-1] = False
            t = 0
            self._T[-1] = 0
            S[-1] = 0

        if np.any(~self._T_isfinite[-self._m :]):
            μ_Q = np.inf
            σ_Q = np.nan
        else:
            μ_Q, σ_Q = core.compute_mean_std(S, self._m)
            μ_Q = μ_Q[0]
            σ_Q = σ_Q[0]

        self._M_T[:-1] = self._M_T[1:]
        self._Σ_T[:-1] = self._Σ_T[1:]
        self._M_T[-1] = μ_Q
        self._Σ_T[-1] = σ_Q

        self._QT_new[1:] = self._QT[:l] - self._T[:l] * t_drop + self._T[self._m :] * t
        self._QT_new[0] = np.sum(self._T[: self._m] * S[: self._m])

        D = core.calculate_distance_profile(
            self._m, self._QT_new, μ_Q, σ_Q, self._M_T, self._Σ_T
        )
        if np.any(~self._T_isfinite[-self._m :]):
            D[:] = np.inf

        core.apply_exclusion_zone(D, D.shape[0] - 1, self._excl_zone, np.inf)

        update_idx = np.argwhere(D < self._P[:, -1]).flatten()
        for i in update_idx:
            idx = np.searchsorted(self._P[i], D[i], side="right")
            core._shift_insert_at_index(self._P[i], idx, D[i])
            core._shift_insert_at_index(
                self._I[i], idx, D.shape[0] + self._n_appended - 1
            )
            # D.shape[0] is base-1

        # Calculate the (top-k) matrix profile values/indices for the last susequence
        # by using its correspondng distance profile `D`
        self._P[-1] = np.inf
        self._I[-1] = -1
        for i, d in enumerate(D):
            if d < self._P[-1, -1]:
                idx = np.searchsorted(self._P[-1], d, side="right")
                core._shift_insert_at_index(self._P[-1], idx, d)
                core._shift_insert_at_index(self._I[-1], idx, i + self._n_appended)

        # All neighbors of the last subsequence are on its left. So, its (top-1)
        # matrix profile value/index and its left matrix profile value/index must
        # be equal.
        self._left_P[-1] = self._P[-1, 0]
        self._left_I[-1] = self._I[-1, 0]

        self._QT[:] = self._QT_new

    def _update(self, t):
        """
        Ingress a new data point and update the (top-k) matrix profile and matrix
        profile indices without egressing the oldest data point

        Parameters
        ----------
        t : float
            A single new data point to be appended to `T`
        """
        n = self._T.shape[0]
        l = n - self._m + 1
        T_new = np.append(self._T, t)
        QT_new = np.empty(self._QT.shape[0] + 1, dtype=np.float64)
        S = T_new[l:]
        t_drop = T_new[l - 1]

        if np.isfinite(t):
            self._T_isfinite = np.append(self._T_isfinite, True)
        else:
            self._T_isfinite = np.append(self._T_isfinite, False)
            t = 0
            T_new[-1] = 0
            S[-1] = 0

        if np.any(~self._T_isfinite[-self._m :]):
            μ_Q = np.inf
            σ_Q = np.nan
        else:
            μ_Q, σ_Q = core.compute_mean_std(S, self._m)
            μ_Q = μ_Q[0]
            σ_Q = σ_Q[0]

        M_T_new = np.append(self._M_T, μ_Q)
        Σ_T_new = np.append(self._Σ_T, σ_Q)

        QT_new[1:] = self._QT[:l] - T_new[:l] * t_drop + T_new[self._m :] * t
        QT_new[0] = np.sum(T_new[: self._m] * S[: self._m])

        D = core.calculate_distance_profile(self._m, QT_new, μ_Q, σ_Q, M_T_new, Σ_T_new)
        if np.any(~self._T_isfinite[-self._m :]):
            D[:] = np.inf

        core.apply_exclusion_zone(D, D.shape[0] - 1, self._excl_zone, np.inf)

        update_idx = np.argwhere(D[:l] < self._P[:l, -1]).flatten()
        for i in update_idx:
            idx = np.searchsorted(self._P[i], D[i], side="right")
            core._shift_insert_at_index(self._P[i], idx, D[i])
            core._shift_insert_at_index(self._I[i], idx, l)

        # Calculating top-k matrix profile and (top-1) left matrix profile (and their
        # corresponding indices) for new subsequence whose distance profie is `D`
        P_new = np.full(self._k, np.inf, dtype=np.float64)
        I_new = np.full(self._k, -1, dtype=np.int64)
        for i, d in enumerate(D):
            if d < P_new[-1]:  # maximum value in sorted array P_new
                idx = np.searchsorted(P_new, d, side="right")
                core._shift_insert_at_index(P_new, idx, d)
                core._shift_insert_at_index(I_new, idx, i)

        left_I_new = I_new[0]
        left_P_new = P_new[0]

        self._T = T_new
        self._P = np.append(self._P, P_new.reshape(1, -1), axis=0)
        self._I = np.append(self._I, I_new.reshape(1, -1), axis=0)
        self._left_P = np.append(self._left_P, left_P_new)
        self._left_I = np.append(self._left_I, left_I_new)
        self._QT = QT_new
        self._M_T = M_T_new
        self._Σ_T = Σ_T_new

    @property
    def P_(self):
        """
        Get the (top-k) matrix profile. When `k=1` (default), the output is
        a 1D array consisting of the matrix profile. When `k > 1`, the
        output is a 2D array that has exactly `k` columns and it consists of the
        top-k matrix profile.
        """
        if self._k == 1:
            return self._P.flatten().astype(np.float64)
        else:
            return self._P.astype(np.float64)

    @property
    def I_(self):
        """
        Get the (top-k) matrix profile indices. When `k=1` (default), the output is
        a 1D array consisting of the matrix profile indices. When `k > 1`, the
        output is a 2D array that has exactly `k` columns and it consists of the
        top-k matrix profile indices.
        """
        if self._k == 1:
            return self._I.flatten().astype(np.int64)
        else:
            return self._I.astype(np.int64)

    @property
    def left_P_(self):
        """
        Get the (top-1) left matrix profile
        """
        return self._left_P.astype(np.float64)

    @property
    def left_I_(self):
        """
        Get the (top-1) left matrix profile indices
        """
        return self._left_I.astype(np.int64)

    @property
    def T_(self):
        """
        Get the time series
        """
        return self._T
